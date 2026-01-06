#!/usr/bin/env python3

"""Run test generation evaluation: generate complete test files from scratch.

Generate complete test files for each instance (interactive with model).

Usage:
python -m minisweagent.run.extra.testgen --subset lite --split dev
"""

import concurrent.futures
import json
import random
import re
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

import typer
import yaml
from datasets import load_dataset
from rich.live import Live

from minisweagent.agents.default import (
	DefaultAgent,
	LimitsExceeded,
	NonTerminatingException,
	TerminatingException,
)
from minisweagent.config import builtin_config_dir, get_config_path
from minisweagent.environments.docker import DockerEnvironment
from minisweagent.models import get_model
from minisweagent.run.extra.utils.batch_progress import RunBatchProgressManager
from minisweagent.run.utils.save import save_traj

app = typer.Typer(rich_markup_mode="rich", add_completion=False)

DATASET_MAPPING = {
	"full": "princeton-nlp/SWE-Bench",
	"verified": "princeton-nlp/SWE-Bench_Verified",
	"lite": "princeton-nlp/SWE-Bench_Lite",
	"multimodal": "princeton-nlp/SWE-Bench_Multimodal",
	"multilingual": "swe-bench/SWE-Bench_Multilingual",
	"smith": "SWE-bench/SWE-smith",
	"_test": "klieret/swe-bench-dummy-test-dataset",
}

_OUTPUT_FILE_LOCK = threading.Lock()
TEST_PATCHES_PATH = Path("/home/sunyuxuan04/mini-swe-agent/test_patches.jsonl")


def get_swebench_docker_image_name(instance: dict) -> str:
	image_name = instance.get("image_name", None)
	if image_name is None:
		iid = instance["instance_id"]
		id_docker_compatible = iid.replace("__", "_1776_")
		image_name = f"swebench/sweb.eval.x86_64.{id_docker_compatible}:latest".lower()
	return image_name


def update_preds_file(output_path: Path, instance_id: str, model_name: str, result: str):
	with _OUTPUT_FILE_LOCK:
		output_data = {}
		if output_path.exists():
			output_data = json.loads(output_path.read_text())
		output_data[instance_id] = {
			"model_name_or_path": model_name,
			"instance_id": instance_id,
			"model_patch": result,
		}
		output_path.write_text(json.dumps(output_data, indent=2))


def remove_from_preds_file(output_path: Path, instance_id: str):
	if not output_path.exists():
		return
	with _OUTPUT_FILE_LOCK:
		output_data = json.loads(output_path.read_text())
		if instance_id in output_data:
			del output_data[instance_id]
			output_path.write_text(json.dumps(output_data, indent=2))


def _parse_list_field(v) -> list[str]:
	if v is None:
		return []
	if isinstance(v, list):
		return [str(x) for x in v]
	if isinstance(v, str):
		s = v.strip()
		try:
			if s.startswith("[") and s.endswith("]"):
				return [str(x) for x in json.loads(s)]
			if "," in s:
				return [x.strip() for x in s.split(",") if x.strip()]
			if s:
				return [s]
		except Exception:
			return [s]
	return []


def _load_test_patches() -> dict[str, dict]:
	if not TEST_PATCHES_PATH.exists():
		return {}
	mapping: dict[str, dict] = {}
	for line in TEST_PATCHES_PATH.read_text().splitlines():
		s = line.strip()
		if not s:
			continue
		obj = json.loads(s)
		instance_id = obj.get("instance_id") or obj.get("id") or obj.get("name")
		if not instance_id:
			continue
		mapping[str(instance_id)] = {
			"patch": obj.get("patch", ""),
			"test_patch": obj.get("test_patch", ""),
			"test_files": _parse_list_field(obj.get("test_files")),
			"files": _parse_list_field(obj.get("files")),
			"F2P": _parse_list_field(obj.get("FAIL_TO_PASS")),
			"P2P": _parse_list_field(obj.get("PASS_TO_PASS")),
		}
	return mapping


class ProgressTrackingEvalAgent(DefaultAgent):
	def __init__(self, *args, progress_manager: RunBatchProgressManager, instance_id: str = "", **kwargs):
		super().__init__(*args, **kwargs)
		self.progress_manager: RunBatchProgressManager = progress_manager
		self.instance_id = instance_id

	def step(self) -> dict:
		self.progress_manager.update_instance_status(
			self.instance_id, f"Step {self.model.n_calls + 1:3d} (${self.model.cost:.2f})"
		)
		return super().step()

	def run(self, task: str, *, max_total_seconds: int | None = None, template_vars: dict | None = None) -> tuple[str, str]:
		start_time = time.time()
		self.messages = []
		self.add_message("system", self.render_template(self.config.system_template))
		template_vars = template_vars or {}
		self.add_message("user", self.render_template(self.config.instance_template, task=task, **template_vars))
		while True:
			if max_total_seconds is not None and (time.time() - start_time) > max_total_seconds:
				raise LimitsExceeded("Instance time limit exceeded")
			try:
				self.step()
			except NonTerminatingException as e:
				self.add_message("user", str(e))
			except TerminatingException as e:
				self.add_message("user", str(e))
				return type(e).__name__, str(e)


def _git_reset_clean(env: DockerEnvironment):
	env.execute("git reset --hard && git clean -fd && git checkout . | cat")


def _clear_test_files(env: DockerEnvironment, test_files: list[str]) -> str:
	"""Clear the content of test files while preserving the files themselves."""
	if not test_files:
		return "No test files to clear"
	
	results = []
	for test_file in test_files:
		# Check if file exists first
		check_result = env.execute(f"test -f {test_file}")
		if check_result.get("returncode", 1) == 0:
			# File exists, clear its content
			clear_result = env.execute(f"truncate -s 0 {test_file}")
			if clear_result.get("returncode", 1) == 0:
				results.append(f"Cleared {test_file}")
			else:
				results.append(f"Failed to clear {test_file}")
		else:
			# File doesn't exist, create empty file
			create_result = env.execute(f"touch {test_file}")
			if create_result.get("returncode", 1) == 0:
				results.append(f"Created empty {test_file}")
			else:
				results.append(f"Failed to create {test_file}")
	
	return "; ".join(results)


def process_instance_generate(
	instance: dict,
	output_dir: Path,
	model_name: str | None,
	config_path: str | Path,
	progress_manager: RunBatchProgressManager,
	base_url: str | None = None,
	api_key: str | None = None,
	self_deployed_ip: str | None = None,
	prompt_builder: str | None = None,
) -> None:
	"""Generate complete test files from scratch (interactive with model)."""
	instance_id = instance["instance_id"]
	instance_dir = output_dir / instance_id
	remove_from_preds_file(output_dir / "test_preds.json", instance_id)
	(instance_dir / f"{instance_id}.traj.json").unlink(missing_ok=True)

	image_name = get_swebench_docker_image_name(instance)
	config = yaml.safe_load(get_config_path(config_path).read_text())

	model_config = config.get("model", {})
	if base_url:
		model_config["base_url"] = base_url
	if api_key:
		model_config.setdefault("model_kwargs", {})["api_key"] = api_key
	if self_deployed_ip:
		model_config["self_deployed_ip"] = self_deployed_ip
	if prompt_builder:
		try:
			import importlib.util
			spec = importlib.util.spec_from_file_location("prompt_builder", prompt_builder)
			prompt_builder_module = importlib.util.module_from_spec(spec)
			spec.loader.exec_module(prompt_builder_module)
			model_config["prompt_builder"] = prompt_builder_module
		except Exception as e:
			print(f"Warning: Failed to load prompt builder from {prompt_builder}: {e}")

	model = get_model(model_name, config=model_config)
	task = instance["problem_statement"]

	progress_manager.on_instance_start(instance_id)
	progress_manager.update_instance_status(instance_id, "Pulling/starting docker")

	agent = None
	env = None
	exit_status: str | None = None
	result: str | None = None

	try:
		env = DockerEnvironment(**(config.get("environment", {}) | {"image": image_name, "timeout": 600}))
		entry = _load_test_patches().get(instance_id, {})

		# Reset repository to clean state
		_git_reset_clean(env)

		test_files: list[str] = entry.get("test_files", [])
		
		# Clear test files content but preserve the files
		if test_files:
			progress_manager.update_instance_status(instance_id, "Clearing test files")
			clear_status = _clear_test_files(env, test_files)
			print(f"Test files clearing status for {instance_id}: {clear_status}")

		agent = ProgressTrackingEvalAgent(
			get_model(model_name, config=model_config),
			env,
			progress_manager=progress_manager,
			instance_id=instance_id,
			**config.get("agent", {}),
		)
		template_vars = {
			"test_files": test_files,
		}

		exit_status, result = agent.run(task, max_total_seconds=50 * 60, template_vars=template_vars)

	except Exception as e:
		print(f"Error processing instance {instance_id}: {str(e)}")
		exit_status, result = "Error", str(e)
	finally:
		save_traj(
			agent,
			instance_dir / f"{instance_id}.traj.json",
			exit_status=exit_status,
			result=result,
			extra_info={"step": "generate"},
			instance_id=instance_id,
		)
		if env is not None:
			env.cleanup()
		try:
			resolved_model_name = model.config.model_name  # type: ignore[attr-defined]
		except Exception:
			resolved_model_name = model_name or ""
		update_preds_file(
			output_dir / "test_preds.json",
			instance_id,
			resolved_model_name,
			result or "",
		)
		progress_manager.on_instance_end(instance_id, exit_status or "Error")


def filter_instances(
	instances: list[dict], *, filter_spec: str, slice_spec: str = "", shuffle: bool = False
) -> list[dict]:
	if shuffle:
		instances = sorted(instances.copy(), key=lambda x: x["instance_id"])
		random.seed(42)
		random.shuffle(instances)
	before_filter = len(instances)
	if filter_spec:
		instances = [instance for instance in instances if re.match(filter_spec, instance["instance_id"])]
	if (after_filter := len(instances)) != before_filter:
		print(f"Instance filter: {before_filter} -> {after_filter} instances")
	if slice_spec:
		values = [int(x) if x else None for x in slice_spec.split(":")]
		instances = instances[slice(*values)]
		if (after_slice := len(instances)) != before_filter:
			print(f"Instance slice: {before_filter} -> {after_slice} instances")
	return instances


@app.command(help="Run test generation: generate complete test files from scratch.")
def main(
	subset: str = typer.Option("lite", "--subset", help="SWEBench subset to use or path to a dataset"),
	split: str = typer.Option("dev", "--split", help="Dataset split"),
	slice_spec: str = typer.Option("", "--slice", help="Slice specification (e.g., '0:5')"),
	filter_spec: str = typer.Option("", "--filter", help="Filter instance IDs by regex"),
	shuffle: bool = typer.Option(False, "--shuffle", help="Shuffle instances"),
	output: str = typer.Option("", "-o", "--output", help="Output directory"),
	workers: int = typer.Option(1, "-w", "--workers", help="Number of worker threads"),
	model: str | None = typer.Option(None, "-m", "--model", help="Model to use"),
	redo_existing: bool = typer.Option(False, "--redo-existing", help="Redo existing instances"),
	skip_existing: bool = typer.Option(False, "--skip-existing", help="Skip instances that already have trajectory files"),
	config: Path = typer.Option(
		builtin_config_dir / "extra" / "testgen.yaml", "-c", "--config", help="Path to a config file"
	),
	base_url: str | None = typer.Option(None, "--base-url", help="Base URL for API calls"),
	api_key: str | None = typer.Option(None, "--api-key", help="API key for model access"),
	self_deployed_ip: str | None = typer.Option(None, "--self-deployed-ip", help="IP address for self-deployed model"),
	prompt_builder: str | None = typer.Option(None, "--prompt-builder", help="Path to prompt builder module"),
	start_index: int = typer.Option(0, "--start-index", help="Start processing from this index"),
	num_instances: int | None = typer.Option(None, "--num-instances", help="Number of instances to process"),
	suffix: str = typer.Option("", "--suffix", help="Optional suffix to append to output directory name"),
) -> None:
	dataset_path = DATASET_MAPPING.get(subset, subset)
	print(f"Loading dataset {dataset_path}, split {split}...")
	instances = list(load_dataset(dataset_path, split=split))

	instances = filter_instances(instances, filter_spec=filter_spec, slice_spec=slice_spec, shuffle=shuffle)
	if start_index > 0 or num_instances is not None:
		end_index = start_index + num_instances if num_instances is not None else None
		instances = instances[start_index:end_index]
		print(f"Processing instances from index {start_index} to {end_index or 'end'}: {len(instances)} instances")

	# Auto output path
	if not output:
		config_data = yaml.safe_load(get_config_path(config).read_text())
		model_config = config_data.get("model", {})
		if base_url:
			model_config["base_url"] = base_url
		if api_key:
			model_config.setdefault("model_kwargs", {})["api_key"] = api_key
		if self_deployed_ip:
			model_config["self_deployed_ip"] = self_deployed_ip
		if prompt_builder:
			try:
				import importlib.util
				spec = importlib.util.spec_from_file_location("prompt_builder", prompt_builder)
				prompt_builder_module = importlib.util.module_from_spec(spec)
				spec.loader.exec_module(prompt_builder_module)
				model_config["prompt_builder"] = prompt_builder_module
			except Exception as e:
				print(f"Warning: Failed to load prompt builder from {prompt_builder}: {e}")
		temp_model = get_model(model, config=model_config)
		model_name = temp_model.config.model_name.replace("/", "_").replace(":", "_")
		output = f"./results/{model_name}_testgen"
		print(f"Auto-generated output path: {output}")

	if suffix:
		p = Path(output)
		output = str(p.parent / f"{p.name}_{suffix}")

	output_path = Path(output)

	# Skip/redo logic
	preds_file = output_path / "test_preds.json"

	if skip_existing:
		existing = []
		for instance in instances:
			iid = instance["instance_id"]
			if preds_file.exists():
				preds_data = json.loads(preds_file.read_text())
				if iid in preds_data:
					# Check if the content is not empty
					instance_data = preds_data[iid]
					if isinstance(instance_data, dict):
						model_patch = instance_data.get("model_patch", "")
					else:
						model_patch = str(instance_data)

					# Only skip if content is not empty
					if model_patch and model_patch.strip():
						existing.append(iid)
		if existing:
			print(f"Skipping {len(existing)} instances that already have results")
			instances = [x for x in instances if x["instance_id"] not in existing]
	elif not redo_existing and preds_file.exists():
		existing_instances = list(json.loads(preds_file.read_text()).keys())
		print(f"Skipping {len(existing_instances)} existing instances from {preds_file.name}")
		instances = [x for x in instances if x["instance_id"] not in existing_instances]

	output_path.mkdir(parents=True, exist_ok=True)
	print(f"Running test generation on {len(instances)} instances...")
	print(f"Results will be saved to {output_path}")

	progress_manager = RunBatchProgressManager(len(instances), output_path / f"exit_statuses_{time.time()}.yaml")

	def process_futures(futures: dict[concurrent.futures.Future, str]):
		for future in concurrent.futures.as_completed(futures):
			try:
				future.result()
			except concurrent.futures.CancelledError:
				pass
			except Exception as e:
				iid = futures[future]
				print(f"Error in future for instance {iid}: {e}")
				traceback.print_exc()
				progress_manager.on_uncaught_exception(iid, e)

	with Live(progress_manager.render_group, refresh_per_second=4):
		with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
			futures = {
				executor.submit(
					process_instance_generate,
					instance,
					output_path,
					model,
					config,
					progress_manager,
					base_url,
					api_key,
					self_deployed_ip,
					prompt_builder,
				): instance["instance_id"]
				for instance in instances
			}
			try:
				process_futures(futures)
			except KeyboardInterrupt:
				print("Cancelling all pending jobs. Press ^C again to exit immediately.")
				for future in futures:
					if not future.running() and not future.done():
						future.cancel()
				process_futures(futures)


if __name__ == "__main__":
	app()