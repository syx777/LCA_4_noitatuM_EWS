#!/usr/bin/env python3

"""Test hack patches against golden patches and record pytest results."""

from __future__ import annotations

import concurrent.futures
import json
import random
import re
import shlex
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Iterable

import typer
from rich.live import Live
import concurrent.futures

import os
swe_bench_path = os.environ.get("SWE_BENCH_PATH", "")
if swe_bench_path:
    sys.path.append(swe_bench_path)

try:
    from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
    from swebench.harness.test_spec.test_spec import make_test_spec
    from swebench.harness.log_parsers import MAP_REPO_TO_PARSER
except ImportError as e:
    sys.exit(1)

from minisweagent.run.extra.utils.batch_progress import RunBatchProgressManager
from minisweagent.environments.docker import DockerEnvironment

_HELP_TEXT = """Test hack patches against golden patches and record pytest results for Python repositories.

[not dim]
- Applies golden patch + hack patch diff
- Tests with golden test patch vs generated test patch
- Records pytest results for comparison
- Uses swe-bench environment configuration and result parsing for Python projects
[/not dim]
"""

app = typer.Typer(rich_markup_mode="rich", add_completion=False)

TEST_PATCHES_PATH = Path(os.environ.get("TEST_PATCHES_PATH", "test_patches.jsonl"))
HACK_PREDS_PATH = Path(os.environ.get("HACK_PREDS_PATH", "hack_preds.json"))

_OUTPUT_FILE_LOCK = threading.Lock()


def _get_total_failures(pytest_results: dict) -> int:
    return pytest_results.get("failed", 0) + pytest_results.get("errors", 0)


def _calculate_rdr_rcs_metrics(h_total: set, h_base: set, h_gen: set) -> dict:
    h_hidden = h_total - h_base
    h_gen_hidden = h_gen - h_base
    h_gen_base = h_gen & h_base
    
    if len(h_hidden) > 0:
        rdr = len(h_gen_hidden) / len(h_hidden)
    else:
        rdr = 0.0
    
    if len(h_base) > 0:
        rcs = len(h_gen_base) / len(h_base)
    else:
        rcs = 0.0
    
    return {
        "rdr": rdr,
        "rcs": rcs,
        "h_total_count": len(h_total),
        "h_base_count": len(h_base),
        "h_gen_count": len(h_gen),
        "h_hidden_count": len(h_hidden),
        "h_gen_hidden_count": len(h_gen_hidden),
        "h_gen_base_count": len(h_gen_base),
        "h_base_list": list(h_base),
        "h_gen_list": list(h_gen),
        "h_gen_hidden_list": list(h_gen_hidden),
        "h_gen_base_list": list(h_gen_base),
    }


def _run_single_test_scenario(env: DockerEnvironment, instance_id: str, test_files: list[str], 
                             code_patch: str = "", test_patch: str = "", 
                             scenario_name: str = "") -> dict:
    _git_reset_clean(env)
    
    if code_patch:
        _write_and_apply_patch(env, code_patch, f"{scenario_name}_code")
    
    if test_patch:
        _write_and_apply_patch(env, test_patch, f"{scenario_name}_test")
    
    test_result = _run_all_tests(env, test_files, instance_id)
    test_result["scenario_name"] = scenario_name
    
    return test_result


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


def _load_all_instance_ids() -> list[str]:
    if not TEST_PATCHES_PATH.exists():
        return []
    ordered_ids: list[str] = []
    for line in TEST_PATCHES_PATH.read_text().splitlines():
        s = line.strip()
        if not s:
            continue
        obj = json.loads(line)
        instance_id = obj.get("instance_id") or obj.get("id") or obj.get("name")
        if instance_id:
            ordered_ids.append(str(instance_id))
    return ordered_ids


def _load_test_patches() -> tuple[dict[str, dict], list[str]]:
    if not TEST_PATCHES_PATH.exists():
        return {}, []
    mapping: dict[str, dict] = {}
    ordered_ids: list[str] = []
    for line in TEST_PATCHES_PATH.read_text().splitlines():
        s = line.strip()
        if not s:
            continue
        obj = json.loads(line)
        instance_id = obj.get("instance_id") or obj.get("id") or obj.get("name")
        if not instance_id:
            continue
        instance_id_str = str(instance_id)
        ordered_ids.append(instance_id_str)
        mapping[instance_id_str] = {
            "patch": obj.get("patch", ""),
            "test_patch": obj.get("test_patch", ""),
            "test_files": _parse_list_field(obj.get("test_files")),
            "files": obj.get("files") or [],
            "F2P": _parse_list_field(obj.get("FAIL_TO_PASS")),
            "P2P": _parse_list_field(obj.get("PASS_TO_PASS")),
        }
    return mapping, ordered_ids


def _filter_diff_for_files(diff_text: str, target_files: list[str]) -> str:
    """Filter a git diff to only include changes for specific files"""
    if not diff_text or not target_files:
        return ""
    
    lines = diff_text.split('\n')
    result_lines = []
    current_file = None
    in_target_file = False
    
    i = 0
    while i < len(lines):
        line = lines[i]
        
        # Check for file headers
        if line.startswith('diff --git'):
            # Extract the file path from the diff header
            # Format: diff --git a/path/to/file b/path/to/file
            parts = line.split()
            if len(parts) >= 4:
                file_path = parts[2][2:]  # Remove 'a/' prefix
                current_file = file_path
                in_target_file = any(file_path == target_file for target_file in target_files)
                
                if in_target_file:
                    result_lines.append(line)
            else:
                in_target_file = False
        elif line.startswith('index ') or line.startswith('---') or line.startswith('+++'):
            # File metadata lines
            if in_target_file:
                result_lines.append(line)
        elif line.startswith('@@'):
            # Hunk header
            if in_target_file:
                result_lines.append(line)
        elif line.startswith('+') or line.startswith('-') or line.startswith(' '):
            # Diff content lines
            if in_target_file:
                result_lines.append(line)
        else:
            # Other lines (empty lines, etc.)
            if in_target_file:
                result_lines.append(line)
        
        i += 1
    
    return '\n'.join(result_lines)


def _exclude_files_from_diff(diff_text: str, exclude_files: list[str]) -> str:
    """Filter a git diff to exclude changes for specific files"""
    if not diff_text:
        return diff_text
    if not exclude_files:
        return diff_text

    lines = diff_text.split('\n')
    result_lines = []
    current_file = None
    in_excluded_file = False

    i = 0
    while i < len(lines):
        line = lines[i]

        # Check for file headers
        if line.startswith('diff --git'):
            # Extract the file path from the diff header
            # Format: diff --git a/path/to/file b/path/to/file
            parts = line.split()
            if len(parts) >= 4:
                file_path = parts[2][2:]  # Remove 'a/' prefix
                current_file = file_path
                in_excluded_file = any(file_path == exclude_file for exclude_file in exclude_files)

                if not in_excluded_file:
                    result_lines.append(line)
            else:
                in_excluded_file = False
                result_lines.append(line)
        elif line.startswith('index ') or line.startswith('---') or line.startswith('+++'):
            # File metadata lines
            if not in_excluded_file:
                result_lines.append(line)
        elif line.startswith('@@'):
            # Hunk header
            if not in_excluded_file:
                result_lines.append(line)
        elif line.startswith('+') or line.startswith('-') or line.startswith(' '):
            # Diff content lines
            if not in_excluded_file:
                result_lines.append(line)
        else:
            # Other lines (empty lines, etc.)
            if not in_excluded_file:
                result_lines.append(line)

        i += 1

    return '\n'.join(result_lines)


def _load_hack_patches() -> dict[str, list[str]]:
    """Load hack patches from preds.json file"""
    if not HACK_PREDS_PATH.exists():
        print(f"Warning: Hack predictions file {HACK_PREDS_PATH} does not exist")
        return {}

    try:
        content = HACK_PREDS_PATH.read_text()
        if not content.strip():
            print(f"Warning: Hack predictions file {HACK_PREDS_PATH} is empty")
            return {}
        data = json.loads(content)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Error: Failed to parse hack predictions file {HACK_PREDS_PATH}: {e}")
        return {}

    hack_patches: dict[str, list[str]] = {}
    
    for instance_id, instance_data in data.items():
        try:
            if isinstance(instance_data, dict) and "model_patch" in instance_data:
                model_patch_str = instance_data.get("model_patch", "")
                if not model_patch_str:
                    hack_patches[instance_id] = []
                    continue
                    
                try:
                    # Parse the JSON string inside model_patch
                    model_patch_data = json.loads(model_patch_str)
                    if isinstance(model_patch_data, dict) and "hacks" in model_patch_data:
                        diff_list = []
                        for hack in model_patch_data.get("hacks", []):
                            if isinstance(hack, dict) and "diff" in hack:
                                # Extract the diff from each hack
                                diff_list.append(hack["diff"])
                        hack_patches[instance_id] = diff_list
                    else:
                        hack_patches[instance_id] = []
                except (json.JSONDecodeError, KeyError, TypeError) as e:
                    print(f"Warning: Failed to parse model_patch for instance {instance_id}: {e}")
                    hack_patches[instance_id] = []
            else:
                hack_patches[instance_id] = []
        except Exception as e:
            print(f"Warning: Error processing instance {instance_id}: {e}")
            hack_patches[instance_id] = []
    return hack_patches


def _load_test_preds(test_preds_path: Path) -> dict[str, str]:
    if not test_preds_path.exists():
        return {}

    try:
        content = test_preds_path.read_text()
        if not content.strip():
            return {}
        data = json.loads(content)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Error: Failed to parse test predictions file {test_preds_path}: {e}")
        return {}

    preds: dict[str, str] = {}
    for instance_id, content in data.items():
        if isinstance(content, dict):
            preds[instance_id] = content.get("model_patch", "")
        else:
            preds[instance_id] = str(content)
    return preds


def _parse_test_output_with_swe_bench(output: str, test_cmd: str = "", test_nodeids: list[str] = None, instance_id: str = "") -> dict:
    passed = failed = errors = skipped = 0
    summary = ""
    
    try:
        if "__" in instance_id:
            org_project = instance_id.split("__")[1].split("-")[0]
            org = instance_id.split("__")[0]
            repo_name = f"{org}/{org_project}"
        else:
            return _parse_test_output(output, test_cmd, test_nodeids)
        
        parser_func = MAP_REPO_TO_PARSER.get(repo_name)
        if parser_func is None:
            return _parse_test_output(output, test_cmd, test_nodeids)
        
        class MockTestSpec:
            def __init__(self, repo, instance_id):
                self.repo = repo
                self.instance_id = instance_id
        
        test_spec = MockTestSpec(repo_name, instance_id)
        test_status_map = parser_func(output, test_spec)
        
        total_passed = sum(1 for status in test_status_map.values() if status in ["PASSED", "PASS"])
        total_failed = sum(1 for status in test_status_map.values() if status in ["FAILED", "FAIL", "ERROR", "ERR"])
        total_skipped = sum(1 for status in test_status_map.values() if status in ["SKIPPED", "SKIP"])
        
        total = total_passed + total_failed + total_skipped
        summary = f"Parsed {total} tests: {total_passed} passed, {total_failed} failed, {total_skipped} skipped"
        
        result = {
            "passed": total_passed,
            "failed": total_failed,
            "errors": 0,
            "skipped": total_skipped,
            "total": total,
            "summary": summary,
            "all_test_status": test_status_map
        }
        
        return result
        
    except Exception as e:
        traceback.print_exc()
        return _parse_test_output(output, test_cmd, test_nodeids)


def _parse_coverage_output(output: str) -> dict:
    coverage_result = {
        "total_coverage": 0.0,
        "file_coverage": {},
        "lines_covered": 0,
        "lines_total": 0,
        "has_coverage_data": False,
        "coverage_summary": "",
        "error": None
    }
    
    try:
        lines = output.split('\n')
        coverage_started = False
        
        for line in lines:
            line_stripped = line.strip()
            
            if "Name" in line_stripped and "Stmts" in line_stripped and "Cover" in line_stripped:
                coverage_started = True
                continue
            
            if coverage_started and "TOTAL" in line_stripped:
                parts = line_stripped.split()
                if len(parts) >= 4:
                    for part in parts:
                        if part.endswith('%'):
                            try:
                                coverage_result["total_coverage"] = float(part[:-1])
                                coverage_result["has_coverage_data"] = True
                            except ValueError:
                                pass
                            break
                    
                    try:
                        if len(parts) >= 3:
                            coverage_result["lines_total"] = int(parts[1])
                            coverage_result["lines_covered"] = int(parts[1]) - int(parts[2])
                    except (ValueError, IndexError):
                        pass
                
                coverage_result["coverage_summary"] = line_stripped
                break
            
            if coverage_started and line_stripped and not line_stripped.startswith('-'):
                parts = line_stripped.split()
                if len(parts) >= 4 and not parts[0] in ["Name", "TOTAL"]:
                    filename = parts[0]
                    for part in parts:
                        if part.endswith('%'):
                            try:
                                file_coverage = float(part[:-1])
                                coverage_result["file_coverage"][filename] = file_coverage
                            except ValueError:
                                pass
                            break
        
        if not coverage_result["has_coverage_data"]:
            for line in lines:
                if "coverage:" in line.lower():
                    import re
                    match = re.search(r'(\d+(?:\.\d+)?)\%', line)
                    if match:
                        coverage_result["total_coverage"] = float(match.group(1))
                        coverage_result["has_coverage_data"] = True
                        coverage_result["coverage_summary"] = line.strip()
                        break
    
    except Exception as e:
        coverage_result["error"] = str(e)
    
    return coverage_result


def _parse_test_output(output: str, test_cmd: str = "", requested_tests: list[str] = None) -> dict:
    passed = failed = errors = skipped = 0
    summary = ""
    
    
    try:
        lines = output.split('\n')
        
        for idx, line in enumerate(reversed(lines)):
            line_stripped = line.strip()
            if not line_stripped:
                continue
            
            if "passed" in line_stripped or "failed" in line_stripped or "error" in line_stripped or "skipped" in line_stripped:
                numbers = re.findall(r'(\d+)\s+(passed|failed|error|errors|skipped)', line_stripped)
                for count, status in numbers:
                    count = int(count)
                    if status == "passed":
                        passed = count
                    elif status == "failed":
                        failed = count
                    elif status in ("error", "errors"):
                        errors = count
                    elif status == "skipped":
                        skipped = count
                
                if numbers:
                    summary = line_stripped
                    break
            
            if "=" in line_stripped and ("in" in line_stripped and "second" in line_stripped):
                content_match = re.search(r'=+\s*(.+?)\s*=*$', line_stripped)
                if content_match:
                    content = content_match.group(1)
                    numbers = re.findall(r'(\d+)\s+(passed|failed|error|errors|skipped)', content)
                    for count, status in numbers:
                        count = int(count)
                        if status == "passed":
                            passed = count
                        elif status == "failed":
                            failed = count
                        elif status in ("error", "errors"):
                            errors = count
                        elif status == "skipped":
                            skipped = count
                    
                    if numbers:
                        summary = line_stripped
                        break
            
            if "Ran" in line_stripped and "test" in line_stripped:
                match = re.search(r'Ran\s+(\d+)\s+test', line_stripped)
                if match:
                    total_tests = int(match.group(1))
                    summary = line_stripped
                    
                    for j in range(max(0, len(lines) - idx - 1), min(len(lines), len(lines) - idx + 5)):
                        next_line = lines[j].strip()
                        
                        if re.match(r'^OK(\s+\((\d+)\s+tests?\))?$', next_line):
                            passed = total_tests
                            failed = errors = skipped = 0
                            summary += f" -> {next_line}"
                            break
                        
                        failed_match = re.match(r'^FAILED\s+\((.+)\)$', next_line)
                        if failed_match:
                            summary_text = failed_match.group(1)
                            failures_m = re.search(r'failures?=(\d+)', summary_text)
                            errors_m = re.search(r'errors?=(\d+)', summary_text)
                            skipped_m = re.search(r'skipped=(\d+)', summary_text)
                            
                            if failures_m:
                                failed = int(failures_m.group(1))
                            if errors_m:
                                errors = int(errors_m.group(1))
                            if skipped_m:
                                skipped = int(skipped_m.group(1))
                            
                            passed = max(0, total_tests - failed - errors - skipped)
                            summary += f" -> {next_line}"
                            break
                    
                    if passed == 0 and failed == 0 and errors == 0:
                        if "OK" in line_stripped:
                            passed = total_tests
                    
                    break
    
    except Exception as e:
        traceback.print_exc()
    
    total = passed + failed + errors + skipped
    result = {
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "skipped": skipped,
        "total": total,
        "summary": summary
    }
    return result


def _filter_instances(instances: list[dict], *, filter_spec: str, slice_spec: str, shuffle: bool) -> list[dict]:
    if shuffle:
        instances = sorted(instances, key=lambda x: x["instance_id"])
        random.seed(42)
        random.shuffle(instances)
    before = len(instances)
    if filter_spec:
        instances = [e for e in instances if re.match(filter_spec, e["instance_id"])]
    if slice_spec:
        values = [int(x) if x else None for x in slice_spec.split(":")]
        instances = instances[slice(*values)]
    return instances




def _git_reset_clean(env: DockerEnvironment):
    env.execute("cd /testbed && git reset --hard && git clean -fd && git checkout . | cat")


def _write_and_apply_patch(env: DockerEnvironment, patch_text: str, label: str) -> str:
    if not patch_text or not patch_text.strip():
        return f"{label} patch: empty (skipped)"
    marker = f"MINI_SWE_AGENT_{label.upper()}_PATCH_EOF_$(date +%s)"
    write_cmd = f"cat > /tmp/{label}.patch << '{marker}'\n{patch_text}\n{marker}"
    wr = env.execute(write_cmd)
    if wr.get("returncode", 1) != 0:
        return f"{label} patch write failed: {wr.get('stderr', '')}"
    
    check_result = env.execute(f"git apply --check -p1 /tmp/{label}.patch 2>&1")
    if check_result.get("returncode", 1) != 0:
        ap = env.execute(f"git apply -p1 --ignore-whitespace /tmp/{label}.patch 2>&1")
        if ap.get("returncode", 1) != 0:
            error_msg = ap.get("stdout", "") + ap.get("stderr", "")
            return f"{label} patch apply failed: {error_msg[:500]}"
    else:
        ap = env.execute(f"git apply -p1 /tmp/{label}.patch 2>&1")
        if ap.get("returncode", 1) != 0:
            error_msg = ap.get("stdout", "") + ap.get("stderr", "")
            return f"{label} patch apply failed: {error_msg[:500]}"
    
    return f"{label} patch applied"


def _setup_validator_env(env: DockerEnvironment, test_spec, instance_id: str = "", f2p_tests: list[str] = None) -> None:
    proxy_url = os.environ.get("PROXY_URL", "")
    proxy_host = os.environ.get("PROXY_HOST", "")
    proxy_port = os.environ.get("PROXY_PORT", "")
    
    if proxy_url:
        env_setup = f"""
export http_proxy={proxy_url}
export https_proxy={proxy_url}
export HTTP_PROXY={proxy_url}
export HTTPS_PROXY={proxy_url}
export LC_ALL=C.UTF-8
export LANG=C.UTF-8
"""
    elif proxy_host and proxy_port:
        env_setup = f"""
export http_proxy=http://{proxy_host}:{proxy_port}
export https_proxy=http://{proxy_host}:{proxy_port}
export HTTP_PROXY=http://{proxy_host}:{proxy_port}
export HTTPS_PROXY=http://{proxy_host}:{proxy_port}
export LC_ALL=C.UTF-8
export LANG=C.UTF-8
"""
    else:
        env_setup = """
export LC_ALL=C.UTF-8
export LANG=C.UTF-8
"""
    env.execute(env_setup)
    
    try:
        if hasattr(test_spec, 'setup_env_script') and test_spec.setup_env_script:
            script_marker = f"MINI_SWE_AGENT_ENV_SCRIPT_EOF_{uuid.uuid4().hex[:8]}"
            write_script_cmd = f"cat > /tmp/setup_env.sh << '{script_marker}'\n{test_spec.setup_env_script}\n{script_marker}"
            
            write_result = env.execute(write_script_cmd)
            if write_result.get("returncode", 1) != 0:
                return
            
            env.execute("chmod +x /tmp/setup_env.sh")
            
            setup_cmd = """
cd /testbed
if [ -d "/opt/miniconda3" ]; then
    source /opt/miniconda3/etc/profile.d/conda.sh
    bash /tmp/setup_env.sh
else
    bash /tmp/setup_env.sh
fi
"""
            
            result = env.execute(setup_cmd)
        else:
            repo = getattr(test_spec, 'repo', '')
            version = getattr(test_spec, 'version', '')
            
            if repo and version:
                from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
                specs = MAP_REPO_VERSION_TO_SPECS.get(repo, {}).get(version, {})
                
                install_commands = specs.get("install", [])
                if isinstance(install_commands, str):
                    install_commands = [install_commands]
                    
                for cmd in install_commands:
                    if cmd:
                        env.execute(f"cd /testbed && {cmd}")
    except Exception as e:
        try:
            if hasattr(test_spec, 'install_repo_script') and test_spec.install_repo_script:
                script_marker = f"MINI_SWE_AGENT_INSTALL_SCRIPT_EOF_{uuid.uuid4().hex[:8]}"
                write_install_cmd = f"cat > /tmp/install_repo.sh << '{script_marker}'\n{test_spec.install_repo_script}\n{script_marker}"
                
                env.execute(write_install_cmd)
                env.execute("chmod +x /tmp/install_repo.sh")
                env.execute("cd /testbed && bash /tmp/install_repo.sh")
        except Exception:
            pass
    



def _run_coverage_tests(env: DockerEnvironment, test_files: list[str], instance_id: str = "", source_dirs: list[str] = None) -> dict:
    if not test_files:
        return {
            "success": False,
            "coverage": {"total_coverage": 0.0, "has_coverage_data": False, "error": "No test files provided"},
            "output": "",
            "error": "No test files provided"
        }
    
    if source_dirs is None:
        source_dirs = []
        if "__" in instance_id:
            parts = instance_id.split("__")
            if len(parts) >= 2:
                org = parts[0]
                project_version = parts[1]
                if "-" in project_version:
                    project = project_version.rsplit("-", 1)[0]
                else:
                    project = project_version
                
                possible_dirs = [
                    project.lower(),
                    f"{project.lower()}/{project.lower()}",
                    "src",
                    f"src/{project.lower()}",
                    "lib",
                    f"lib/{project.lower()}",
                ]
                
                if project.lower() == "astropy":
                    possible_dirs.insert(0, "astropy")
                elif project.lower() == "matplotlib":
                    possible_dirs.insert(0, "lib/matplotlib")
                elif project.lower() == "scikit-learn":
                    possible_dirs.insert(0, "sklearn")
                elif project.lower() == "xarray":
                    possible_dirs.insert(0, "xarray")
                
                source_dirs = possible_dirs[:3]
        
        if not source_dirs:
            source_dirs = [".", "src", "lib"]
    
    test_files_str = " ".join(test_files)
    coverage_commands = []
    
    for source_dir in source_dirs[:2]:
        coverage_commands.append(f"pytest --cov={source_dir} --cov-report=term-missing {test_files_str}")
        coverage_commands.append(f"coverage run -m pytest {test_files_str} && coverage report")
    
    if not coverage_commands:
        coverage_commands = [
            f"pytest --cov=. --cov-report=term-missing {test_files_str}",
            f"coverage run -m pytest {test_files_str} && coverage report"
        ]
    
    success = False
    coverage_output = ""
    coverage_result = {}
    
        for cmd_idx, base_cmd in enumerate(coverage_commands):
        try:
            cmd = f"source /opt/miniconda3/bin/activate testbed && cd /testbed && {base_cmd}"
            cmd = f"export PYTHONIOENCODING=utf-8 && export LC_ALL=C.UTF-8 && export LANG=C.UTF-8 && {cmd}"
            cmd = f"export PYTHONWARNINGS='ignore::DeprecationWarning' && {cmd}"
            test_timeout = 900
            cmd = f"timeout {test_timeout} bash -c {shlex.quote(cmd)}"
            
            result = env.execute(f"{cmd} 2>&1 | cat")
            output = result.get("output", "")
            returncode = result.get("returncode", 1)
            
            coverage_result = _parse_coverage_output(output)
            
            if coverage_result.get("has_coverage_data", False):
                success = True
                coverage_output = output
                break
            else:
                if cmd_idx == 0:
                    coverage_output = output
                    
        except Exception as e:
            if cmd_idx == 0:
                coverage_result["error"] = str(e)
                coverage_output = f"Error: {e}"
    
    return {
        "success": success,
        "coverage": coverage_result,
        "output": coverage_output,
        "commands_tried": len(coverage_commands),
        "source_dirs": source_dirs,
        "test_files": test_files
    }


def _run_tests_with_cmd(env: DockerEnvironment, test_cmd: str, test_nodeids: list[str] = None, instance_id: str = "") -> tuple[bool, dict]:
    if not test_cmd:
        return True, {"test_results": {}, "output": ""}
    
    cmd = test_cmd
    is_chained_redirection = '&&' in cmd and '>' in cmd and 'cat' in cmd
    if is_chained_redirection:
        pass
    
    cmd = f"source /opt/miniconda3/bin/activate testbed && cd /testbed && {cmd}"
    
    if "runtests.py" in cmd or "django" in instance_id.lower():
        cmd = f"export PYTHONIOENCODING=utf-8 && export LC_ALL=C.UTF-8 && export LANG=C.UTF-8 && {cmd}"
    
    if "pytest" in cmd:
        cmd = f"export PYTHONWARNINGS='ignore::DeprecationWarning' && {cmd}"
    
    test_timeout = 900
    cmd = f"timeout {test_timeout} bash -c {shlex.quote(cmd)}"
    
    try:
        res = env.execute(f"{cmd} 2>&1 | cat")
        output = res.get("output", "")
        returncode = res.get("returncode", 1)
        
        if returncode == 124:
            return False, {
                "test_results": {"passed": 0, "failed": 0, "errors": 1, "skipped": 0, "total": 1, "summary": f"Test execution timed out after {test_timeout} seconds"},
                "output": f"Test execution timed out after {test_timeout} seconds\n{output}",
                "requested_tests": test_nodeids,
                "timeout": True
            }
            
    except Exception as e:
        error_msg = str(e)
        
        if "timed out" in error_msg.lower() or "timeout" in error_msg.lower():
            return False, {
                "test_results": {"passed": 0, "failed": 0, "errors": 1, "skipped": 0, "total": 1, "summary": "Test execution timed out"},
                "output": f"Test execution timed out: {error_msg}",
                "requested_tests": test_nodeids,
                "timeout": True
            }
        else:
            return False, {
                "test_results": {"passed": 0, "failed": 0, "errors": 1, "skipped": 0, "total": 1, "summary": f"Test execution error: {error_msg[:200]}"},
                "output": error_msg,
                "requested_tests": test_nodeids,
                "error": True
            }
    
    test_results = _parse_test_output_with_swe_bench(output, test_cmd, test_nodeids, instance_id)
    
    return returncode == 0, {
        "test_results": test_results,
        "output": output,
        "requested_tests": test_nodeids
    }


def _run_all_tests(env: DockerEnvironment, test_files: list[str], instance_id: str = "") -> dict:
    if not test_files:
        return {
            "success": True,
            "test_results": {"passed": 0, "failed": 0, "errors": 0, "skipped": 0, "total": 0, "summary": ""},
            "output": "",
            "failed_tests": [],
            "total_tests": 0,
        }

    try:
        mock_instance = {"instance_id": instance_id}
        
        for line in TEST_PATCHES_PATH.read_text().splitlines():
            if line.strip():
                data = json.loads(line)
                data_instance_id = data.get("instance_id", data.get("id", data.get("name")))
                if data_instance_id == instance_id:
                    mock_instance.update({
                        "repo": data.get("repo", ""),
                        "base_commit": data.get("base_commit", "main"),
                        "patch": data.get("patch", ""),
                        "test_patch": data.get("test_patch", ""),
                        "problem_statement": data.get("problem_statement", ""),
                        "hints_text": data.get("hints_text", ""),
                        "version": data.get("version", ""),
                        "FAIL_TO_PASS": data.get("FAIL_TO_PASS", []),
                        "PASS_TO_PASS": data.get("PASS_TO_PASS", []),
                    })
                    break
        
        if not mock_instance.get("repo"):
            if "__" in instance_id:
                parts = instance_id.split("__")
                if len(parts) >= 2:
                    org = parts[0]
                    project_version = parts[1]
                    if "-" in project_version:
                        project = project_version.rsplit("-", 1)[0]
                        version = project_version.rsplit("-", 1)[1]
                    else:
                        project = project_version
                        version = ""
                    
                    repo = f"{org}/{project}"
                    
                    if repo == "astropy/astropy" and version.isdigit():
                        version = "5.0"
                    elif repo == "django/django" and version.isdigit():
                        version_num = int(version)
                        if version_num < 10000:
                            version = "1.11"
                        elif version_num < 15000:
                            version = "3.2"
                        elif version_num < 20000:
                            version = "4.2"
                        else:
                            version = "5.0"
                    elif repo == "matplotlib/matplotlib" and version.isdigit():
                        version_num = int(version)
                        if version_num < 20000:
                            version = "3.0"
                        elif version_num < 25000:
                            version = "3.5"
                        else:
                            version = "3.7"
                    elif repo == "pydata/xarray" and version.isdigit():
                        version_num = int(version)
                        if version_num < 5000:
                            version = "0.12"
                        elif version_num < 6500:
                            version = "0.19"
                        else:
                            version = "2022.03"
                    elif repo == "scikit-learn/scikit-learn" and version.isdigit():
                        version_num = int(version)
                        if version_num < 10000:
                            version = "0.20"
                        elif version_num < 13000:
                            version = "0.22"
                        else:
                            version = "1.3"
                    elif repo == "sympy/sympy" and version.isdigit():
                        version_num = int(version)
                        if version_num < 15000:
                            version = "1.1"
                        elif version_num < 20000:
                            version = "1.5"
                        else:
                            version = "1.9"
                    elif repo == "sphinx-doc/sphinx" and version.isdigit():
                        version_num = int(version)
                        if version_num < 5000:
                            version = "1.8"
                        elif version_num < 7500:
                            version = "3.0"
                        else:
                            version = "4.0"
                    
                    mock_instance.update({
                        "repo": repo,
                        "version": version,
                        "base_commit": "main",
                        "patch": "",
                        "test_patch": "",
                        "problem_statement": "",
                        "hints_text": "",
                        "FAIL_TO_PASS": [],
                        "PASS_TO_PASS": [],
                    })
        
        test_spec = make_test_spec(mock_instance)
        
        try:
            repo = getattr(test_spec, 'repo', '')
            version = getattr(test_spec, 'version', '')
            if repo and version:
                specs = MAP_REPO_VERSION_TO_SPECS.get(repo, {}).get(version, {})
                test_cmd = specs.get("test_cmd", "")
                if isinstance(test_cmd, list):
                    test_cmd = " && ".join(test_cmd) if test_cmd else ""
                    
                if not test_cmd:
                    test_cmd = "pytest -rA"
            else:
                test_cmd = "pytest -rA"
                
            if test_files:
                if "django" in repo.lower() and "./tests/runtests.py" in test_cmd:
                    django_test_modules = []
                    for test_file in test_files:
                        if test_file.endswith('.py') and test_file.startswith('tests/'):
                            relative_path = test_file[6:]
                            module_path = relative_path[:-3].replace('/', '.')
                            django_test_modules.append(module_path)
                        elif test_file.startswith('tests/') and not test_file.endswith('.py'):
                            relative_path = test_file[6:]
                            dir_path = '/'.join(relative_path.split('/')[:-1])
                            if dir_path:
                                module_path = dir_path.replace('/', '.')
                                if module_path not in django_test_modules:
                                    django_test_modules.append(module_path)
                    
                    if django_test_modules:
                        test_cmd_with_files = f"{test_cmd} {' '.join(django_test_modules)}"
                    else:
                        test_cmd_with_files = test_cmd
                else:
                    test_files_str = " ".join(test_files)
                    test_cmd_with_files = f"{test_cmd} {test_files_str}"
            else:
                test_cmd_with_files = test_cmd
                
            success, test_info = _run_tests_with_cmd(env, test_cmd_with_files, None, instance_id)
            test_results = test_info.get("test_results", {})
            output = test_info.get("output", "")
            
        except Exception as e:
            test_results = {"passed": 0, "failed": 1, "errors": 0, "skipped": 0, "total": 1, "summary": f"Error: {str(e)[:200]}"}
            output = str(e)
            success = False

        failed_tests = []
        if hasattr(test_results, 'get') and test_results.get('all_test_status'):
            all_test_status = test_results.get('all_test_status', {})
            failed_tests = [test_name for test_name, status in all_test_status.items() 
                          if status in ["FAILED", "FAIL", "ERROR", "ERR"]]
        
        total_tests = test_results.get('total', 0)

    except Exception as e:
        return {
            "success": False,
            "test_results": {"passed": 0, "failed": 1, "errors": 0, "skipped": 0, "total": 1, "summary": f"Error: {str(e)[:200]}"},
            "output": str(e),
            "failed_tests": [],
            "total_tests": 0,
        }

    return {
        "success": success,
        "test_results": test_results,
        "output": output,
        "failed_tests": failed_tests,
        "total_tests": total_tests,
    }




def _update_common_results(base_output_dir: Path, instance_id: str, results: dict, models_to_process: list[str]):
    with _OUTPUT_FILE_LOCK:
        for model_name in models_to_process:
            model_dir = base_output_dir / model_name
            model_dir.mkdir(parents=True, exist_ok=True)
            output_path = model_dir / "results.json"
            
            output_data = {}
            if output_path.exists():
                try:
                    content = output_path.read_text()
                    if content.strip():
                        output_data = json.loads(content)
                except (json.JSONDecodeError, ValueError):
                    output_data = {}
            
            output_data[instance_id] = results
            output_path.write_text(json.dumps(output_data, indent=2))

def _update_model_results(base_output_dir: Path, instance_id: str, model_name: str, results: dict):
    with _OUTPUT_FILE_LOCK:
        model_dir = base_output_dir / model_name
        model_dir.mkdir(parents=True, exist_ok=True)
        output_path = model_dir / "results.json"
        
        output_data = {}
        if output_path.exists():
            try:
                content = output_path.read_text()
                if content.strip():
                    output_data = json.loads(content)
            except (json.JSONDecodeError, ValueError):
                output_data = {}
        
        if instance_id not in output_data:
            output_data[instance_id] = {}
        
        output_data[instance_id].update(results)
        output_path.write_text(json.dumps(output_data, indent=2))


def _save_step_results(base_output_dir: Path, step_name: str, instance_id: str, results: dict, model_name: str = None):
    with _OUTPUT_FILE_LOCK:
        if model_name:
            step_dir = base_output_dir / f"{step_name}_model"
            step_dir.mkdir(parents=True, exist_ok=True)
            output_path = step_dir / f"{model_name.replace('/', '_').replace(':', '_')}.json"
        else:
            output_path = base_output_dir / f"{step_name}.json"
        
        output_data = {}
        if output_path.exists():
            try:
                content = output_path.read_text()
                if content.strip():
                    output_data = json.loads(content)
            except (json.JSONDecodeError, ValueError):
                output_data = {}
        
        output_data[instance_id] = results
        output_path.write_text(json.dumps(output_data, indent=2))


def _check_step_results_exist(base_output_dir: Path, step_name: str, instance_id: str, model_name: str = None) -> bool:
    try:
        if model_name:
            output_path = base_output_dir / f"{step_name}_model" / f"{model_name.replace('/', '_').replace(':', '_')}.json"
        else:
            output_path = base_output_dir / f"{step_name}.json"
        
        if not output_path.exists():
            return False
        
        content = output_path.read_text()
        if not content.strip():
            return False
        output_data = json.loads(content)
        return instance_id in output_data
    except (json.JSONDecodeError, ValueError, FileNotFoundError, PermissionError):
        return False
    except Exception:
        return False


def process_step1_golden_validation(
    entry: dict,
    output_dir: Path,
    progress_manager: RunBatchProgressManager,
    timeout: int,
    image_override: str | None = None,
    container_timeout: str = "10m",
) -> bool:
    instance_id: str = entry["instance_id"]
    
    if _check_step_results_exist(output_dir, "step1", instance_id):
        step1_path = output_dir / "step1.json"
        if step1_path.exists():
            try:
                all_step1_data = json.loads(step1_path.read_text())
                step1_data = all_step1_data.get(instance_id, {})
                step1_passed = step1_data.get("passed", False)
                return step1_passed
            except (KeyError, json.JSONDecodeError, FileNotFoundError):
                return False
        return False
    
    test_patches, _ = _load_test_patches()
    if instance_id not in test_patches:
        progress_manager.update_instance_status(instance_id, "No test patch found")
        return False
    
    instance_data = test_patches[instance_id]
    
    f2p: list[str] = [t for t in instance_data.get("F2P", []) if t]
    p2p: list[str] = [t for t in instance_data.get("P2P", []) if t]
    
    test_files = set()
    for test_node in f2p + p2p:
        if "::" in test_node:
            test_file = test_node.split("::")[0]
            test_files.add(test_file)
    test_files = list(test_files)

    if not test_files:
        test_files = instance_data.get("test_files", [])

    progress_manager.on_instance_start(instance_id)
    progress_manager.update_instance_status(instance_id, "Step1: Golden validation")
    
    image_name = image_override or get_swebench_docker_image_name(instance_id)
    env = DockerEnvironment(
        image=image_name,
        timeout=timeout,
        cwd="/testbed",
        container_timeout=container_timeout
    )
    
    try:
        mock_instance = {"instance_id": instance_id}
        
        for line in TEST_PATCHES_PATH.read_text().splitlines():
            if line.strip():
                data = json.loads(line)
                data_instance_id = data.get("instance_id", data.get("id", data.get("name")))
                if data_instance_id == instance_id:
                    mock_instance.update({
                        "repo": data.get("repo", ""),
                        "base_commit": data.get("base_commit", "main"),
                        "patch": data.get("patch", ""),
                        "test_patch": data.get("test_patch", ""),
                        "problem_statement": data.get("problem_statement", ""),
                        "hints_text": data.get("hints_text", ""),
                        "version": data.get("version", ""),
                        "FAIL_TO_PASS": data.get("FAIL_TO_PASS", []),
                        "PASS_TO_PASS": data.get("PASS_TO_PASS", []),
                    })
                    break
        
        if not mock_instance.get("repo"):
            if "__" in instance_id:
                parts = instance_id.split("__")
                if len(parts) >= 2:
                    org = parts[0]
                    project_version = parts[1]
                    if "-" in project_version:
                        project = project_version.rsplit("-", 1)[0]
                        version = project_version.rsplit("-", 1)[1]
                    else:
                        project = project_version
                        version = ""
                    
                    repo = f"{org}/{project}"
                    
                    if repo == "astropy/astropy" and version.isdigit():
                        version = "5.0"
                    elif repo == "django/django" and version.isdigit():
                        version_num = int(version)
                        if version_num < 10000:
                            version = "1.11"
                        elif version_num < 15000:
                            version = "3.2"
                        elif version_num < 20000:
                            version = "4.2"
                        else:
                            version = "5.0"
                    elif repo == "matplotlib/matplotlib" and version.isdigit():
                        version_num = int(version)
                        if version_num < 20000:
                            version = "3.0"
                        elif version_num < 25000:
                            version = "3.5"
                        else:
                            version = "3.7"
                    elif repo == "pydata/xarray" and version.isdigit():
                        version_num = int(version)
                        if version_num < 5000:
                            version = "0.12"
                        elif version_num < 6500:
                            version = "0.19"
                        else:
                            version = "2022.03"
                    elif repo == "scikit-learn/scikit-learn" and version.isdigit():
                        version_num = int(version)
                        if version_num < 10000:
                            version = "0.20"
                        elif version_num < 13000:
                            version = "0.22"
                        else:
                            version = "1.3"
                    elif repo == "sympy/sympy" and version.isdigit():
                        version_num = int(version)
                        if version_num < 15000:
                            version = "1.1"
                        elif version_num < 20000:
                            version = "1.5"
                        else:
                            version = "1.9"
                    elif repo == "sphinx-doc/sphinx" and version.isdigit():
                        version_num = int(version)
                        if version_num < 5000:
                            version = "1.8"
                        elif version_num < 7500:
                            version = "3.0"
                        else:
                            version = "4.0"
                    
                    mock_instance.update({
                        "repo": repo,
                        "version": version,
                        "base_commit": "main",
                        "patch": "",
                        "test_patch": "",
                        "problem_statement": "",
                        "hints_text": "",
                        "FAIL_TO_PASS": [],
                        "PASS_TO_PASS": [],
                    })
        
        test_spec = make_test_spec(mock_instance)
        _setup_validator_env(env, test_spec, instance_id, f2p_tests=f2p)
        
        step1_result = _run_single_test_scenario(
            env, instance_id, test_files, 
            code_patch=instance_data["patch"], 
            test_patch=instance_data["test_patch"],
            scenario_name="step1_golden_validation"
        )
        
        step1_passed = step1_result["success"] and step1_result["test_results"].get("failed", 0) == 0
        
        step1_data = {
            "result": step1_result,
            "passed": step1_passed,
            "timestamp": time.time(),
        }
        _save_step_results(output_dir, "step1", instance_id, step1_data)
        
        if step1_passed:
            progress_manager.update_instance_status(instance_id, "Step1: PASSED")
        else:
            progress_manager.update_instance_status(instance_id, "Step1: FAILED - Golden patches have issues")
        
        return step1_passed
        
    except Exception as e:
        progress_manager.update_instance_status(instance_id, f"Step1: Error: {e}")
        error_data = {
            "error": str(e),
            "passed": False,
            "timestamp": time.time(),
        }
        _save_step_results(output_dir, "step1", instance_id, error_data)
        return False
    finally:
        env.cleanup()




def _check_common_results_exist(output_path: Path, instance_id: str) -> bool:
    if not output_path.exists():
        return False

    try:
        content = output_path.read_text()
        if not content.strip():
            return False
        output_data = json.loads(content)
        if instance_id not in output_data:
            return False
        
        instance_data = output_data[instance_id]
        required_keys = ["step1_golden_validation", "step3_hack_results", "h_base", "h_total"]
        return all(key in instance_data for key in required_keys)
    except (json.JSONDecodeError, ValueError, KeyError):
        return False

def _check_model_results_exist(base_output_dir: Path, instance_id: str, model_name: str) -> bool:
    model_dir = base_output_dir / model_name
    output_path = model_dir / "results.json"
    
    if not output_path.exists():
        return False

    try:
        content = output_path.read_text()
        if not content.strip():
            return False
        output_data = json.loads(content)
        if instance_id not in output_data:
            return False
            
        instance_data = output_data[instance_id]
        required_keys = ["step2_generated_test_validation", "step4_hack_results", "h_gen", "metrics"]
        return all(key in instance_data for key in required_keys)
    except (json.JSONDecodeError, ValueError, KeyError):
        return False


def get_swebench_docker_image_name(instance_id: str) -> str:
    iid = instance_id.replace("__", "_1776_").lower()
    return f"swebench/sweb.eval.x86_64.{iid}:latest"


def process_common_steps(
    entry: dict,
    output_dir: Path,
    progress_manager: RunBatchProgressManager,
    timeout: int,
    image_override: str | None = None,
    container_timeout: str = "10m",
) -> tuple[bool, dict]:
    instance_id: str = entry["instance_id"]
    
    test_patches, _ = _load_test_patches()
    hack_patches = _load_hack_patches()
    
    if instance_id not in test_patches:
        progress_manager.update_instance_status(instance_id, "No test patch found, skipping")
        return False, {}
    
    if instance_id not in hack_patches:
        progress_manager.update_instance_status(instance_id, "No hack patches found, skipping")
        return False, {}
    
    instance_data = test_patches[instance_id]
    instance_hacks = hack_patches[instance_id]
    
    f2p: list[str] = [t for t in instance_data.get("F2P", []) if t]
    p2p: list[str] = [t for t in instance_data.get("P2P", []) if t]
    
    test_files = set()
    for test_node in f2p + p2p:
        if "::" in test_node:
            test_file = test_node.split("::")[0]
            test_files.add(test_file)
    test_files = list(test_files)

    if not test_files:
        test_files = instance_data.get("test_files", [])

    progress_manager.on_instance_start(instance_id)
    progress_manager.update_instance_status(instance_id, f"Processing common steps for {len(instance_hacks)} hack patches")
    
    image_name = image_override or get_swebench_docker_image_name(instance_id)
    env = DockerEnvironment(
        image=image_name,
        timeout=timeout,
        cwd="/testbed",
        container_timeout=container_timeout
    )
    
    try:
        mock_instance = {"instance_id": instance_id}
        
        for line in TEST_PATCHES_PATH.read_text().splitlines():
            if line.strip():
                data = json.loads(line)
                data_instance_id = data.get("instance_id", data.get("id", data.get("name")))
                if data_instance_id == instance_id:
                    mock_instance.update({
                        "repo": data.get("repo", ""),
                        "base_commit": data.get("base_commit", "main"),
                        "patch": data.get("patch", ""),
                        "test_patch": data.get("test_patch", ""),
                        "problem_statement": data.get("problem_statement", ""),
                        "hints_text": data.get("hints_text", ""),
                        "version": data.get("version", ""),
                        "FAIL_TO_PASS": data.get("FAIL_TO_PASS", []),
                        "PASS_TO_PASS": data.get("PASS_TO_PASS", []),
                    })
                    break
        
        if not mock_instance.get("repo"):
            if "__" in instance_id:
                parts = instance_id.split("__")
                if len(parts) >= 2:
                    org = parts[0]
                    project_version = parts[1]
                    if "-" in project_version:
                        project = project_version.rsplit("-", 1)[0]
                        version = project_version.rsplit("-", 1)[1]
                    else:
                        project = project_version
                        version = ""
                    
                    repo = f"{org}/{project}"
                    
                    if repo == "astropy/astropy" and version.isdigit():
                        version = "5.0"
                    elif repo == "django/django" and version.isdigit():
                        version_num = int(version)
                        if version_num < 10000:
                            version = "1.11"
                        elif version_num < 15000:
                            version = "3.2"
                        elif version_num < 20000:
                            version = "4.2"
                        else:
                            version = "5.0"
                    elif repo == "matplotlib/matplotlib" and version.isdigit():
                        version_num = int(version)
                        if version_num < 20000:
                            version = "3.0"
                        elif version_num < 25000:
                            version = "3.5"
                        else:
                            version = "3.7"
                    elif repo == "pydata/xarray" and version.isdigit():
                        version_num = int(version)
                        if version_num < 5000:
                            version = "0.12"
                        elif version_num < 6500:
                            version = "0.19"
                        else:
                            version = "2022.03"
                    elif repo == "scikit-learn/scikit-learn" and version.isdigit():
                        version_num = int(version)
                        if version_num < 10000:
                            version = "0.20"
                        elif version_num < 13000:
                            version = "0.22"
                        else:
                            version = "1.3"
                    elif repo == "sympy/sympy" and version.isdigit():
                        version_num = int(version)
                        if version_num < 15000:
                            version = "1.1"
                        elif version_num < 20000:
                            version = "1.5"
                        else:
                            version = "1.9"
                    elif repo == "sphinx-doc/sphinx" and version.isdigit():
                        version_num = int(version)
                        if version_num < 5000:
                            version = "1.8"
                        elif version_num < 7500:
                            version = "3.0"
                        else:
                            version = "4.0"
                    
                    repo_info = {
                        "repo": repo,
                        "version": version,
                        "base_commit": mock_instance.get("base_commit", "main"),
                    }
                    
                    for key in ["patch", "test_patch", "problem_statement", "hints_text", "FAIL_TO_PASS", "PASS_TO_PASS"]:
                        if not mock_instance.get(key):
                            repo_info[key] = "" if key in ["patch", "test_patch", "problem_statement", "hints_text"] else []
                    
                    mock_instance.update(repo_info)
        
        test_spec = make_test_spec(mock_instance)
        _setup_validator_env(env, test_spec, instance_id, f2p_tests=f2p)
        
        progress_manager.update_instance_status(instance_id, "Step 1: Validating golden patches...")
        step1_result = _run_single_test_scenario(
            env, instance_id, test_files, 
            code_patch=instance_data["patch"], 
            test_patch=instance_data["test_patch"],
            scenario_name="step1_golden_validation"
        )
        
        if not step1_result["success"] or step1_result["test_results"].get("failed", 0) > 0:
            progress_manager.update_instance_status(instance_id, "Step 1 failed - golden patches have issues")
            return False, {}
        
        progress_manager.update_instance_status(instance_id, "Step 3: Testing hacks with original tests...")
        h_base = set()
        step3_results = {}
        
        for hack_idx, hack_diff in enumerate(instance_hacks, 1):
            hack_id = f"hack_{hack_idx}"
            
            test_files_to_exclude = instance_data.get("test_files", [])
            filtered_hack_diff = _exclude_files_from_diff(hack_diff, test_files_to_exclude)
            
            if not filtered_hack_diff.strip():
                continue
            
            step3_result = _run_single_test_scenario(
                env, instance_id, test_files,
                code_patch=filtered_hack_diff, 
                test_patch="",
                scenario_name=f"step3_hack_{hack_idx}_vs_original_test"
            )
            
            step3_results[hack_id] = step3_result
            
            if not step3_result["success"] or len(step3_result["failed_tests"]) > 0:
                h_base.add(hack_id)
        
        h_total = set(f"hack_{i}" for i in range(1, len(instance_hacks) + 1))
        common_results = {
            "step1_golden_validation": step1_result,
            "step3_hack_results": step3_results,
            "h_total": list(h_total),
            "h_base": list(h_base),
            "timestamp": time.time(),
        }
        
        progress_manager.update_instance_status(instance_id, f"Common steps completed: H_total={len(h_total)}, H_base={len(h_base)}")
        
        return True, {
            "instance_data": instance_data,
            "instance_hacks": instance_hacks,
            "test_files": test_files,
            "f2p": f2p,
            "p2p": p2p,
            "h_total": h_total,
            "h_base": h_base,
            "step1_result": step1_result,
            "step3_results": step3_results,
        }
        
    except Exception as e:
        progress_manager.update_instance_status(instance_id, f"Error in common steps: {e}")
        return False, {}
    finally:
        env.cleanup()


def process_model_specific_steps(
    entry: dict,
    common_data: dict,
    model_name: str,
    test_pred: str,
    output_dir: Path,
    progress_manager: RunBatchProgressManager,
    timeout: int,
    image_override: str | None = None,
    container_timeout: str = "10m",
) -> bool:
    instance_id: str = entry["instance_id"]
    
    try:
        instance_data = common_data.get("instance_data", {})
        instance_hacks = common_data.get("instance_hacks", [])
        test_files = common_data.get("test_files", [])
        f2p = common_data.get("f2p", [])
        p2p = common_data.get("p2p", [])
        h_total = common_data.get("h_total", set())
        h_base = common_data.get("h_base", set())
        
        if not instance_data or not instance_hacks:
            return False
            
    except Exception as e:
        return False
    
    progress_manager.on_instance_start(instance_id)
    progress_manager.update_instance_status(instance_id, f"Processing model {model_name} specific steps...")
    
    image_name = image_override or get_swebench_docker_image_name(instance_id)
    env = DockerEnvironment(
        image=image_name,
        timeout=timeout,
        cwd="/testbed",
        container_timeout=container_timeout
    )
    
    try:
        mock_instance = {"instance_id": instance_id}
        
        for line in TEST_PATCHES_PATH.read_text().splitlines():
            if line.strip():
                data = json.loads(line)
                data_instance_id = data.get("instance_id", data.get("id", data.get("name")))
                if data_instance_id == instance_id:
                    mock_instance.update({
                        "repo": data.get("repo", ""),
                        "base_commit": data.get("base_commit", "main"),
                        "patch": data.get("patch", ""),
                        "test_patch": data.get("test_patch", ""),
                        "problem_statement": data.get("problem_statement", ""),
                        "hints_text": data.get("hints_text", ""),
                        "version": data.get("version", ""),
                        "FAIL_TO_PASS": data.get("FAIL_TO_PASS", []),
                        "PASS_TO_PASS": data.get("PASS_TO_PASS", []),
                    })
                    break
        
        if not mock_instance.get("repo"):
            if "__" in instance_id:
                parts = instance_id.split("__")
                if len(parts) >= 2:
                    org = parts[0]
                    project_version = parts[1]
                    if "-" in project_version:
                        project = project_version.rsplit("-", 1)[0]
                        version = project_version.rsplit("-", 1)[1]
                    else:
                        project = project_version
                        version = ""
                    
                    repo = f"{org}/{project}"
                    if repo == "astropy/astropy" and version.isdigit():
                        version = "5.0"
                    elif repo == "django/django" and version.isdigit():
                        version_num = int(version)
                        if version_num < 10000:
                            version = "1.11"
                        elif version_num < 15000:
                            version = "3.2"
                        elif version_num < 20000:
                            version = "4.2"
                        else:
                            version = "5.0"
                    elif repo == "matplotlib/matplotlib" and version.isdigit():
                        version_num = int(version)
                        if version_num < 20000:
                            version = "3.0"
                        elif version_num < 25000:
                            version = "3.5"
                        else:
                            version = "3.7"
                    elif repo == "pydata/xarray" and version.isdigit():
                        version_num = int(version)
                        if version_num < 5000:
                            version = "0.12"
                        elif version_num < 6500:
                            version = "0.19"
                        else:
                            version = "2022.03"
                    elif repo == "scikit-learn/scikit-learn" and version.isdigit():
                        version_num = int(version)
                        if version_num < 10000:
                            version = "0.20"
                        elif version_num < 13000:
                            version = "0.22"
                        else:
                            version = "1.3"
                    elif repo == "sympy/sympy" and version.isdigit():
                        version_num = int(version)
                        if version_num < 15000:
                            version = "1.1"
                        elif version_num < 20000:
                            version = "1.5"
                        else:
                            version = "1.9"
                    elif repo == "sphinx-doc/sphinx" and version.isdigit():
                        version_num = int(version)
                        if version_num < 5000:
                            version = "1.8"
                        elif version_num < 7500:
                            version = "3.0"
                        else:
                            version = "4.0"
                    
                    mock_instance.update({
                        "repo": repo,
                        "version": version,
                        "base_commit": "main",
                        "patch": "",
                        "test_patch": "",
                        "problem_statement": "",
                        "hints_text": "",
                        "FAIL_TO_PASS": [],
                        "PASS_TO_PASS": [],
                    })
        
        test_spec = make_test_spec(mock_instance)
        _setup_validator_env(env, test_spec, instance_id, f2p_tests=f2p)
        
        progress_manager.update_instance_status(instance_id, f"Step 2: Testing {model_name} generated test with golden code...")
        step2_result = _run_single_test_scenario(
            env, instance_id, test_files,
            code_patch=instance_data["patch"], 
            test_patch=test_pred,
            scenario_name="step2_generated_test_validation"
        )
        
        if not step2_result["success"] or step2_result["test_results"].get("failed", 0) > 0:
            progress_manager.update_instance_status(instance_id, f"Step 2 failed - {model_name} test kills golden code")
        
        progress_manager.update_instance_status(instance_id, f"Step 4: Testing hacks with {model_name} generated tests...")
        h_gen = set()
        step4_results = {}
        
        for hack_idx, hack_diff in enumerate(instance_hacks, 1):
            hack_id = f"hack_{hack_idx}"
            
            test_files_to_exclude = instance_data.get("test_files", [])
            filtered_hack_diff = _exclude_files_from_diff(hack_diff, test_files_to_exclude)
            
            if not filtered_hack_diff.strip():
                continue
            
            step4_result = _run_single_test_scenario(
                env, instance_id, test_files,
                code_patch=filtered_hack_diff, 
                test_patch=test_pred,
                scenario_name=f"step4_hack_{hack_idx}_vs_generated_test"
            )
            
            step4_results[hack_id] = step4_result
            
            if not step4_result["success"] or len(step4_result["failed_tests"]) > 0:
                h_gen.add(hack_id)
        
        metrics = _calculate_rdr_rcs_metrics(h_total, h_base, h_gen)
        
        model_results = {
            "step2_generated_test_validation": step2_result,
            "step4_hack_results": step4_results,
            "h_gen": list(h_gen),
            "metrics": metrics,
            "timestamp": time.time(),
        }
        
        _update_model_results(output_dir, instance_id, model_name, model_results)
        
        progress_manager.update_instance_status(
            instance_id, 
            f"Model {model_name} completed: RDR={metrics['rdr']:.3f}, RCS={metrics['rcs']:.3f}, H_gen={len(h_gen)}"
        )
        
        return True
        
    except Exception as e:
        progress_manager.update_instance_status(instance_id, f"Error for model {model_name}: {e}")
        return False
    finally:
        env.cleanup()


def calculate_and_display_results(base_output_path: Path, models_to_process: list[str]) -> dict:
    """
    Calculate and display results based on existing results.json files.
    
    Returns:
        dict: Statistics for all models
    """
    all_model_results = {}
    
    model_dirs = [d for d in base_output_path.iterdir() if d.is_dir()]
    if not model_dirs:
        return all_model_results
    
    for current_model in models_to_process:
        model_results_file = base_output_path / current_model / "results.json"
        
        if not model_results_file.exists():
            continue
        
        try:
            content = model_results_file.read_text()
            if not content.strip():
                continue
            
            data = json.loads(content)
            
            model_stats = {
                "total_instances": 0, 
                "valid_instances": 0, 
                "avg_rdr": 0.0, 
                "avg_rcs": 0.0, 
                "total_hacks": 0, 
                "h_base_total": 0, 
                "h_gen_total": 0
            }
            
            total_instances = 0
            valid_instances = 0
            rdr_sum = 0.0
            rcs_sum = 0.0
            total_hacks = 0
            h_base_total = 0
            h_gen_total = 0

            for instance_id, instance_data in data.items():
                if "metrics" in instance_data:
                    total_instances += 1
                    metrics = instance_data["metrics"]
                    
                    if metrics["h_total_count"] > 0:
                        valid_instances += 1
                        rdr_sum += metrics["rdr"]
                        rcs_sum += metrics["rcs"]
                        total_hacks += metrics["h_total_count"]
                        h_base_total += metrics["h_base_count"]
                        h_gen_total += metrics["h_gen_count"]

            model_stats["total_instances"] = total_instances
            model_stats["valid_instances"] = valid_instances
            model_stats["total_hacks"] = total_hacks
            model_stats["h_base_total"] = h_base_total
            model_stats["h_gen_total"] = h_gen_total

            if valid_instances > 0:
                avg_rdr = rdr_sum / valid_instances
                avg_rcs = rcs_sum / valid_instances
                model_stats["avg_rdr"] = avg_rdr
                model_stats["avg_rcs"] = avg_rcs
            
            all_model_results[current_model] = model_stats
            
        except (json.JSONDecodeError, ValueError) as e:
            pass
        except Exception as e:
            pass

    if all_model_results:
        overall_total_instances = 0
        overall_valid_instances = 0
        overall_rdr_sum = 0.0
        overall_rcs_sum = 0.0
        overall_total_hacks = 0
        overall_h_base_total = 0
        overall_h_gen_total = 0

        for model_name, stats in all_model_results.items():
            overall_total_instances += stats['total_instances']
            overall_valid_instances += stats['valid_instances']
            overall_total_hacks += stats['total_hacks']
            overall_h_base_total += stats['h_base_total']
            overall_h_gen_total += stats['h_gen_total']
            overall_rdr_sum += stats['avg_rdr'] * stats['valid_instances']
            overall_rcs_sum += stats['avg_rcs'] * stats['valid_instances']
    
    return all_model_results


def process_instance_deprecated():
    """This function has been replaced by process_common_steps and process_model_specific_steps"""
    raise NotImplementedError("This function has been replaced. Use process_common_steps and process_model_specific_steps instead.")


@app.command("coverage", help="Run code coverage calculation for generated test patches")
def coverage_command(
    filter_spec: str = typer.Option("", "--filter", help="Filter instance IDs by regex"),
    slice_spec: str = typer.Option("", "--slice", help="Slice like 'start:stop[:step]'"),
    shuffle: bool = typer.Option(False, "--shuffle", help="Shuffle instances deterministically"),
    output: str = typer.Option("", "-o", "--output", help="Base output directory"),
    workers: int = typer.Option(1, "-w", "--workers", help="Number of worker threads"),
    timeout: int = typer.Option(900, "--timeout", help="Per-command timeout seconds"),
    container_timeout: str = typer.Option("10m", "--container-timeout", help="Container max lifetime (e.g. 10m, 1h)"),
    image: str | None = typer.Option(None, "--image", "-i", help="Override docker image"),
    start_index: int = typer.Option(0, "--start-index", help="Start index after filtering"),
    num_instances: int | None = typer.Option(None, "--num-instances", help="Number of instances to process"),
    suffix: str = typer.Option("", "--suffix", help="Optional suffix appended to output directory name"),
    test_model: str = typer.Option("anthropic.claude-sonnet-4", "--test-model", help="Single model name for test predictions path (deprecated, use --test-models)"),
    test_models: str = typer.Option("", "--test-models", help="Comma-separated list of model names for test predictions. If empty, uses --test-model"),
    skip_existing: bool = typer.Option(False, "--skip-existing", help="Skip instances that already exist in output coverage results"),
) -> None:
    """
    Code coverage calculation workflow:
    1. Start container and configure environment
    2. Apply model-generated test patches and golden code patches
    3. Run tests and calculate code coverage
    4. Save coverage results to separate JSON files
    
    Output structure:
    - coverage_model/model_name.json: Coverage results for each model
    """
    
    models_to_process = []
    if test_models.strip():
        models_to_process = [m.strip() for m in test_models.split(",") if m.strip()]
    else:
        models_to_process = [test_model]

    all_instance_ids = _load_all_instance_ids()
    if not all_instance_ids:
        raise typer.Exit(code=1)

    base_output = output or "./results/coverage_testing"
    if suffix:
        p = Path(base_output)
        base_output = str(p.parent / f"{p.name}_{suffix}")
    
    base_output_path = Path(base_output)
    base_output_path.mkdir(parents=True, exist_ok=True)
    
    selected_instance_ids = all_instance_ids.copy()
    if start_index > 0 or num_instances is not None:
        end_index = start_index + num_instances if num_instances is not None else None
        selected_instance_ids = selected_instance_ids[start_index:end_index]
    
    test_patches, _ = _load_test_patches()
    
    instances_for_processing = []
    for instance_id in selected_instance_ids:
        if instance_id in test_patches:
            instances_for_processing.append({"instance_id": instance_id})
    
    instances_for_processing = _filter_instances(instances_for_processing, filter_spec=filter_spec, slice_spec=slice_spec, shuffle=shuffle)
    
    for model_idx, current_model in enumerate(models_to_process, 1):
        test_preds_base = os.environ.get("TEST_PREDS_BASE_PATH", "results/test_generation_new")
        test_preds_path = Path(f"{test_preds_base}/{current_model}/test_preds.json")
        test_preds = _load_test_preds(test_preds_path)
        if not test_preds:
            continue
        
        instances_for_model = []
        for entry in instances_for_processing:
            instance_id = entry["instance_id"]
            if instance_id in test_preds:
                if skip_existing and _check_step_results_exist(base_output_path, "coverage", instance_id, current_model):
                    continue
                instances_for_model.append(entry)
        
        if not instances_for_model:
            continue
        
        coverage_progress_manager = RunBatchProgressManager(len(instances_for_model), base_output_path / f"coverage_{current_model.replace('/', '_').replace(':', '_')}_progress_{time.time()}.yaml")
        coverage_completed_instances = []
        
        def process_coverage_futures(futures: dict[concurrent.futures.Future, str]):
            for future in concurrent.futures.as_completed(futures):
                try:
                    success = future.result()
                    iid = futures[future]
                    if success:
                        coverage_completed_instances.append({"instance_id": iid})
                except Exception as e:
                    iid = futures[future]
                    coverage_progress_manager.on_uncaught_exception(iid, e)
        
        with Live(coverage_progress_manager.render_group, refresh_per_second=4):
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                coverage_futures = {
                    executor.submit(
                        process_coverage_calculation,
                        entry,
                        current_model,
                        base_output_path,
                        coverage_progress_manager,
                        timeout,
                        image_override=image,
                        container_timeout=container_timeout,
                    ): entry["instance_id"]
                    for entry in instances_for_model
                }
                process_coverage_futures(coverage_futures)
    
    _display_coverage_results(base_output_path, models_to_process)


def _display_coverage_results(base_output_path: Path, models_to_process: list[str]):
    """Display final code coverage calculation results"""
    
    for model_name in models_to_process:
        coverage_path = base_output_path / "coverage_model" / f"{model_name.replace('/', '_').replace(':', '_')}.json"
        
        if not coverage_path.exists():
            continue
        
        try:
            coverage_data = json.loads(coverage_path.read_text())
            
            total_instances = len(coverage_data)
            successful_instances = 0
            total_coverage = 0.0
            total_lines_covered = 0
            total_lines_total = 0
            coverage_distribution = {"0-20%": 0, "20-40%": 0, "40-60%": 0, "60-80%": 0, "80-100%": 0}
            
            for instance_id, instance_data in coverage_data.items():
                success = instance_data.get("success", False)
                coverage_percent = instance_data.get("total_coverage", 0.0)
                lines_covered = instance_data.get("lines_covered", 0)
                lines_total = instance_data.get("lines_total", 0)
                
                if success:
                    successful_instances += 1
                    total_coverage += coverage_percent
                    total_lines_covered += lines_covered
                    total_lines_total += lines_total
                    
                    if coverage_percent <= 20:
                        coverage_distribution["0-20%"] += 1
                    elif coverage_percent <= 40:
                        coverage_distribution["20-40%"] += 1
                    elif coverage_percent <= 60:
                        coverage_distribution["40-60%"] += 1
                    elif coverage_percent <= 80:
                        coverage_distribution["60-80%"] += 1
                    else:
                        coverage_distribution["80-100%"] += 1
            
        except Exception as e:
            pass


@app.command(help=_HELP_TEXT)
def main(
    filter_spec: str = typer.Option("", "--filter", help="Filter instance IDs by regex"),
    slice_spec: str = typer.Option("", "--slice", help="Slice like 'start:stop[:step]'"),
    shuffle: bool = typer.Option(False, "--shuffle", help="Shuffle instances deterministically"),
    output: str = typer.Option("", "-o", "--output", help="Base output directory"),
    workers: int = typer.Option(1, "-w", "--workers", help="Number of worker threads"),
    timeout: int = typer.Option(900, "--timeout", help="Per-command timeout seconds"),
    container_timeout: str = typer.Option("10m", "--container-timeout", help="Container max lifetime (e.g. 10m, 1h)"),
    image: str | None = typer.Option(None, "--image", "-i", help="Override docker image"),
    start_index: int = typer.Option(0, "--start-index", help="Start index after filtering"),
    num_instances: int | None = typer.Option(None, "--num-instances", help="Number of instances to process"),
    suffix: str = typer.Option("", "--suffix", help="Optional suffix appended to output directory name"),
    test_model: str = typer.Option("anthropic.claude-sonnet-4", "--test-model", help="Single model name for test predictions path (deprecated, use --test-models)"),
    test_models: str = typer.Option("", "--test-models", help="Comma-separated list of model names for test predictions. If empty, uses --test-model"),
    skip_existing: bool = typer.Option(False, "--skip-existing", help="Skip instances that already exist in output results.json file"),
) -> None:
    models_to_process = []
    if test_models.strip():
        models_to_process = [m.strip() for m in test_models.split(",") if m.strip()]
    else:
        models_to_process = [test_model]

    all_instance_ids = _load_all_instance_ids()
    if not all_instance_ids:
        raise typer.Exit(code=1)

    base_output = output or "./results/hack_testing_four_phase"
    if suffix:
        p = Path(base_output)
        base_output = str(p.parent / f"{p.name}_{suffix}")
    
    base_output_path = Path(base_output)
    base_output_path.mkdir(parents=True, exist_ok=True)
    
    selected_instance_ids = all_instance_ids.copy()
    if start_index > 0 or num_instances is not None:
        end_index = start_index + num_instances if num_instances is not None else None
        selected_instance_ids = selected_instance_ids[start_index:end_index]
    
    test_patches, _ = _load_test_patches()
    hack_patches = _load_hack_patches()
    
    instances_for_processing = []
    for instance_id in selected_instance_ids:
        if instance_id in test_patches and instance_id in hack_patches:
            instances_for_processing.append({"instance_id": instance_id})
    
    instances_for_processing = _filter_instances(instances_for_processing, filter_spec=filter_spec, slice_spec=slice_spec, shuffle=shuffle)
    
    step1_progress = RunBatchProgressManager(len(instances_for_processing), base_output_path / f"step1_progress_{time.time()}.yaml")
    step1_passed_instances = []
    
    def process_step1_futures(futures: dict[concurrent.futures.Future, str]):
        for future in concurrent.futures.as_completed(futures):
            try:
                success = future.result()
                iid = futures[future]
                if success:
                    step1_passed_instances.append({"instance_id": iid})
            except Exception as e:
                iid = futures[future]
                step1_progress.on_uncaught_exception(iid, e)
    
    with Live(step1_progress.render_group, refresh_per_second=4):
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            step1_futures = {
                executor.submit(
                    process_step1_golden_validation,
                    entry,
                    base_output_path,
                    step1_progress,
                    timeout,
                    image_override=image,
                    container_timeout=container_timeout,
                ): entry["instance_id"]
                for entry in instances_for_processing
            }
            process_step1_futures(step1_futures)
    
    
    step3_progress = RunBatchProgressManager(len(step1_passed_instances), base_output_path / f"step3_progress_{time.time()}.yaml")
    step3_completed_instances = []
    
    def process_step3_futures(futures: dict[concurrent.futures.Future, str]):
        for future in concurrent.futures.as_completed(futures):
            try:
                success = future.result()
                iid = futures[future]
                if success:
                    step3_completed_instances.append({"instance_id": iid})
            except Exception as e:
                iid = futures[future]
                step3_progress.on_uncaught_exception(iid, e)
    
    with Live(step3_progress.render_group, refresh_per_second=4):
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            step3_futures = {
                executor.submit(
                    process_step3_hack_testing,
                    entry,
                    base_output_path,
                    step3_progress,
                    timeout,
                    image_override=image,
                    container_timeout=container_timeout,
                ): entry["instance_id"]
                for entry in step1_passed_instances
            }
            process_step3_futures(step3_futures)
    
    step2_passed_instances_by_model = {}
    
    for model_name in models_to_process:
        
        step2_progress = RunBatchProgressManager(len(step1_passed_instances), base_output_path / f"step2_{model_name.replace('/', '_')}_progress_{time.time()}.yaml")
        step2_passed_instances = []
        
        def process_step2_futures(futures: dict[concurrent.futures.Future, str]):
            for future in concurrent.futures.as_completed(futures):
                try:
                    success, generated_patch = future.result()
                    iid = futures[future]
                    if success:
                        step2_passed_instances.append({"instance_id": iid})
                except Exception as e:
                    iid = futures[future]
                    step2_progress.on_uncaught_exception(iid, e)
        
        with Live(step2_progress.render_group, refresh_per_second=4):
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                step2_futures = {
                    executor.submit(
                        process_step2_generated_test_validation,
                        entry,
                        model_name,
                        base_output_path,
                        step2_progress,
                        timeout,
                        image_override=image,
                        container_timeout=container_timeout,
                    ): entry["instance_id"]
                    for entry in step1_passed_instances
                }
                process_step2_futures(step2_futures)
        
        step2_passed_instances_by_model[model_name] = step2_passed_instances
    
    for model_name in models_to_process:
        step2_passed = step2_passed_instances_by_model.get(model_name, [])
        if not step2_passed:
            continue
            
        step4_progress = RunBatchProgressManager(len(step2_passed), base_output_path / f"step4_{model_name.replace('/', '_')}_progress_{time.time()}.yaml")
        step4_completed_instances = []
        
        def process_step4_futures(futures: dict[concurrent.futures.Future, str]):
            for future in concurrent.futures.as_completed(futures):
                try:
                    success = future.result()
                    iid = futures[future]
                    if success:
                        step4_completed_instances.append({"instance_id": iid})
                except Exception as e:
                    iid = futures[future]
                    step4_progress.on_uncaught_exception(iid, e)
        
        with Live(step4_progress.render_group, refresh_per_second=4):
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                step4_futures = {
                    executor.submit(
                        process_step4_generated_test_hack_testing,
                        entry,
                        model_name,
                        base_output_path,
                        step4_progress,
                        timeout,
                        image_override=image,
                        container_timeout=container_timeout,
                    ): entry["instance_id"]
                    for entry in step2_passed
                }
                process_step4_futures(step4_futures)
    
    _display_four_phase_results(base_output_path, models_to_process)


def _display_four_phase_results(base_output_path: Path, models_to_process: list[str]):
    """Display final results of four-phase testing"""
    
    for model_name in models_to_process:
        step2a_path = base_output_path / "step2a_model" / f"{model_name.replace('/', '_').replace(':', '_')}.json"
        step2a_irr_total = 0.0
        step2a_total_instances = 0
        
        instance_irr_status = {}
        
        if step2a_path.exists():
            try:
                step2a_data = json.loads(step2a_path.read_text())
                step2a_total_instances = len(step2a_data)
                
                for instance_id, instance_data in step2a_data.items():
                    irr_value = instance_data.get("irr", 0.0)
                    step2a_irr_total += irr_value
                    instance_irr_status[instance_id] = (irr_value == 1.0)
                        
                avg_irr = step2a_irr_total / step2a_total_instances if step2a_total_instances > 0 else 0.0
                
            except Exception as e:
                pass
        else:
            avg_irr = 0.0
        
        step2_path = base_output_path / "step2_model" / f"{model_name.replace('/', '_').replace(':', '_')}.json"
        step2_total_instances = 0
        step2_passed_instances = 0
        
        instance_vr_status = {}
        
        if step2_path.exists():
            try:
                step2_data = json.loads(step2_path.read_text())
                step2_total_instances = len(step2_data)
                
                for instance_id, instance_data in step2_data.items():
                    vr_passed = instance_data.get("passed", False)
                    if vr_passed:
                        step2_passed_instances += 1
                    instance_vr_status[instance_id] = vr_passed
                        
            except Exception as e:
                pass
        
        step4_path = base_output_path / "step4_model" / f"{model_name.replace('/', '_').replace(':', '_')}.json"
        if not step4_path.exists():
            continue
        
        try:
            step4_data = json.loads(step4_path.read_text())
            
            total_instances = len(step4_data)
            total_rdr = 0.0
            total_h_base = 0
            total_h_gen = 0
            total_hacks = 0
            
            for instance_id, instance_data in step4_data.items():
                metrics = instance_data.get("metrics", {})
                total_rdr += metrics.get("rdr", 0.0)
                total_h_base += metrics.get("h_base_count", 0)
                total_h_gen += metrics.get("h_gen_count", 0)
                total_hacks += metrics.get("h_total_count", 0)
            
            avg_rdr = total_rdr / total_instances if total_instances > 0 else 0.0
            avg_vr = step2_passed_instances / step2_total_instances if step2_total_instances > 0 else 0.0
            
            both_passed_count = 0
            total_common_instances = 0
            
            common_instances = set(instance_irr_status.keys()) & set(instance_vr_status.keys())
            total_common_instances = len(common_instances)
            
            for instance_id in common_instances:
                irr_passed = instance_irr_status.get(instance_id, False)
                vr_passed = instance_vr_status.get(instance_id, False)
                if irr_passed and vr_passed:
                    both_passed_count += 1
            
            avg_combined = both_passed_count / total_common_instances if total_common_instances > 0 else 0.0
            
        except Exception as e:
            pass


def main_legacy(
    filter_spec: str = typer.Option("", "--filter", help="Filter instance IDs by regex"),
    slice_spec: str = typer.Option("", "--slice", help="Slice like 'start:stop[:step]'"),
    shuffle: bool = typer.Option(False, "--shuffle", help="Shuffle instances deterministically"),
    output: str = typer.Option("", "-o", "--output", help="Base output directory"),
    workers: int = typer.Option(1, "-w", "--workers", help="Number of worker threads"),
    timeout: int = typer.Option(900, "--timeout", help="Per-command timeout seconds"),
    container_timeout: str = typer.Option("10m", "--container-timeout", help="Container max lifetime (e.g. 10m, 1h)"),
    image: str | None = typer.Option(None, "--image", "-i", help="Override docker image"),
    start_index: int = typer.Option(0, "--start-index", help="Start index after filtering"),
    num_instances: int | None = typer.Option(None, "--num-instances", help="Number of instances to process"),
    suffix: str = typer.Option("", "--suffix", help="Optional suffix appended to output directory name"),
    test_model: str = typer.Option("anthropic.claude-sonnet-4", "--test-model", help="Single model name for test predictions path (deprecated, use --test-models)"),
    test_models: str = typer.Option("", "--test-models", help="Comma-separated list of model names for test predictions. If empty, uses --test-model"),
    skip_existing: bool = typer.Option(False, "--skip-existing", help="Skip instances that already exist in output results.json file"),
    skip_common: bool = typer.Option(False, "--skip-common", help="Skip common steps (step 1 and 3) if they already exist"),
) -> None:
    models_to_process = []
    if test_models.strip():
        models_to_process = [m.strip() for m in test_models.split(",") if m.strip()]
    else:
        models_to_process = [test_model]

    all_instance_ids = _load_all_instance_ids()
    if not all_instance_ids:
        raise typer.Exit(code=1)

    test_patches, test_patches_order = _load_test_patches()
    if not test_patches:
        raise typer.Exit(code=1)
    
    hack_patches = _load_hack_patches()
    if not hack_patches:
        raise typer.Exit(code=1)
    
    base_output = output or "./results/hack_testing"
    if suffix:
        p = Path(base_output)
        base_output = str(p.parent / f"{p.name}_{suffix}")
    
    base_output_path = Path(base_output)
    base_output_path.mkdir(parents=True, exist_ok=True)
    
    selected_instance_ids = all_instance_ids.copy()
    
    if start_index > 0 or num_instances is not None:
        end_index = start_index + num_instances if num_instances is not None else None
        selected_instance_ids = selected_instance_ids[start_index:end_index]
    
    instances_for_common = []
    
    for instance_id in selected_instance_ids:
        if instance_id in test_patches and instance_id in hack_patches:
            if skip_common and _check_common_results_exist(base_output_path / "results.json", instance_id):
                continue
            instances_for_common.append({"instance_id": instance_id})
    
    instances_for_common = _filter_instances(instances_for_common, filter_spec=filter_spec, slice_spec=slice_spec, shuffle=shuffle)
    
    common_progress_manager = RunBatchProgressManager(len(instances_for_common), base_output_path / f"common_exit_statuses_{time.time()}.yaml")
    common_data_cache = {}
    
    def process_common_futures(futures: dict[concurrent.futures.Future, str]):
        for future in concurrent.futures.as_completed(futures):
            try:
                success, common_data = future.result()
                iid = futures[future]
                if success:
                    common_data_cache[iid] = common_data
                else:
                    print(f"[WARNING] Common step processing failed for instance {iid}")
            except concurrent.futures.CancelledError:
                pass
            except Exception as e:
                iid = futures[future]
                print(f"Error in common step future for instance {iid}: {e}")
                common_progress_manager.on_uncaught_exception(iid, e)
    
    with Live(common_progress_manager.render_group, refresh_per_second=4):
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            common_futures = {
                executor.submit(
                    process_common_steps,
                    entry,
                    base_output_path,
                    common_progress_manager,
                    timeout,
                    image_override=image,
                    container_timeout=container_timeout,
                ): entry["instance_id"]
                for entry in instances_for_common
            }
            try:
                process_common_futures(common_futures)
            except KeyboardInterrupt:
                print("Cancelling all pending common step jobs. Press ^C again to exit immediately.")
                for future in common_futures:
                    if not future.running() and not future.done():
                        future.cancel()
                process_common_futures(common_futures)
    
    for instance_id, common_data in common_data_cache.items():
        try:
            common_results = {
                "step1_golden_validation": common_data["step1_result"],
                "step3_hack_results": common_data["step3_results"],
                "h_total": common_data["h_total"],
                "h_base": common_data["h_base"],
                "timestamp": time.time(),
            }
            _update_common_results(base_output_path, instance_id, common_results, models_to_process)
        except Exception as e:
            pass
    
    all_model_results = {}
    
    for model_idx, current_model in enumerate(models_to_process, 1):
        test_preds_base = os.environ.get("TEST_PREDS_BASE_PATH", "results/test_generation_new")
        test_preds_path = Path(f"{test_preds_base}/{current_model}/test_preds.json")
        test_preds = _load_test_preds(test_preds_path)
        if not test_preds:
            continue
    
        instances_for_model = []
        
        for instance_id in common_data_cache.keys():
            if instance_id in test_preds:
                if skip_existing and _check_model_results_exist(base_output_path, instance_id, current_model):
                    continue
                instances_for_model.append({"instance_id": instance_id})
    
        model_progress_manager = RunBatchProgressManager(len(instances_for_model), base_output_path / f"model_{current_model.replace('/', '_').replace(':', '_')}_exit_statuses_{time.time()}.yaml")
    
        def process_model_futures(futures: dict[concurrent.futures.Future, str]):
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except concurrent.futures.CancelledError:
                    pass
                except Exception as e:
                    iid = futures[future]
                    model_progress_manager.on_uncaught_exception(iid, e)
    
        with Live(model_progress_manager.render_group, refresh_per_second=4):
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                model_futures = {}
                for entry in instances_for_model:
                    instance_id = entry["instance_id"]
                    if instance_id not in common_data_cache:
                        continue
                    if instance_id not in test_preds:
                        continue
                    
                    future = executor.submit(
                        process_model_specific_steps,
                        entry,
                        common_data_cache[instance_id],
                        current_model,
                        test_preds[instance_id],
                        base_output_path,
                        model_progress_manager,
                        timeout,
                        image_override=image,
                        container_timeout=container_timeout,
                    )
                    model_futures[future] = instance_id
                try:
                    process_model_futures(model_futures)
                except KeyboardInterrupt:
                    for future in model_futures:
                        if not future.running() and not future.done():
                            future.cancel()
                    process_model_futures(model_futures)
    
    calculate_and_display_results(base_output_path, models_to_process)


def process_step3_hack_testing(
    entry: dict,
    output_dir: Path,
    progress_manager: RunBatchProgressManager,
    timeout: int,
    image_override: str | None = None,
    container_timeout: str = "10m",
) -> bool:
    """
    Step3: Process hack patches vs original tests
    
    Test hack patches against original test files without applying any test patches,
    to determine which hacks are visible mutants (detectable by original tests)
    
    Returns:
        bool: Whether test completed successfully (used to determine h_base)
    """
    instance_id: str = entry["instance_id"]
    
    if not _check_step_results_exist(output_dir, "step1", instance_id):
        return False
    
    if _check_step_results_exist(output_dir, "step3", instance_id):
        step3_path = output_dir / "step3.json"
        if step3_path.exists():
            try:
                all_step3_data = json.loads(step3_path.read_text())
                step3_data = all_step3_data.get(instance_id, {})
                if "h_total" in step3_data and "h_base" in step3_data:
                    return True
                else:
                    return False
            except (KeyError, json.JSONDecodeError, FileNotFoundError) as e:
                return False
        return False
    
    test_patches, _ = _load_test_patches()
    hack_data = _load_hack_patches()
    
    if instance_id not in test_patches or instance_id not in hack_data:
        progress_manager.update_instance_status(instance_id, "No test data found")
        return False
    
    instance_data = test_patches.get(instance_id, {})
    hack_patches = hack_data.get(instance_id, [])
    
    if not instance_data or not hack_patches:
        progress_manager.update_instance_status(instance_id, f"Empty data - instance_data: {bool(instance_data)}, hack_patches: {bool(hack_patches)}")
        return False
    
    f2p: list[str] = [t for t in instance_data.get("F2P", []) if t]
    p2p: list[str] = [t for t in instance_data.get("P2P", []) if t]
    
    test_files = set()
    for test_node in f2p + p2p:
        if "::" in test_node:
            test_file = test_node.split("::")[0]
            test_files.add(test_file)
    test_files = list(test_files)

    if not test_files:
        test_files = instance_data.get("test_files", [])

    progress_manager.on_instance_start(instance_id)
    progress_manager.update_instance_status(instance_id, "Step3: Hack testing")
    
    image_name = image_override or get_swebench_docker_image_name(instance_id)
    env = DockerEnvironment(
        image=image_name,
        timeout=timeout,
        cwd="/testbed",
        container_timeout=container_timeout
    )
    
    try:
        mock_instance = _create_mock_instance(instance_id)
        test_spec = make_test_spec(mock_instance)
        _setup_validator_env(env, test_spec, instance_id, f2p_tests=f2p)
        
        step3_results = {}
        for hack_idx, hack_patch in enumerate(hack_patches, 1):
            hack_id = f"hack_{hack_idx}"
            
            test_files_to_exclude = instance_data.get("test_files", [])
            filtered_hack_patch = _exclude_files_from_diff(hack_patch, test_files_to_exclude)
            
            if not filtered_hack_patch.strip():
                continue
            
            hack_result = _run_single_test_scenario(
                env, instance_id, test_files,
                code_patch=filtered_hack_patch,
                test_patch="",
                scenario_name=f"step3_hack_{hack_id}_vs_original_test"
            )
            step3_results[hack_id] = hack_result
        
        h_total = set(f"hack_{i}" for i in range(1, len(hack_patches) + 1))
        h_base = set()
        for hack_id, result in step3_results.items():
            if not result.get("success", False) or result.get("test_results", {}).get("failed", 0) > 0:
                h_base.add(hack_id)
        
        step3_data = {
            "hack_results": step3_results,
            "h_total": list(h_total),
            "h_base": list(h_base),
            "timestamp": time.time(),
        }
        _save_step_results(output_dir, "step3", instance_id, step3_data)
        
        progress_manager.update_instance_status(instance_id, f"Step3: COMPLETED ({len(h_base)}/{len(h_total)} hacks killed)")
        return True
        
    except Exception as e:
        progress_manager.update_instance_status(instance_id, f"Step3: Error: {e}")
        error_data = {
            "error": str(e),
            "h_total": [],
            "h_base": [],
            "timestamp": time.time(),
        }
        _save_step_results(output_dir, "step3", instance_id, error_data)
        return False
    finally:
        env.cleanup()


def process_step2_generated_test_validation(
    entry: dict,
    model_name: str,
    output_dir: Path,
    progress_manager: RunBatchProgressManager,
    timeout: int,
    image_override: str | None = None,
    container_timeout: str = "10m",
) -> tuple[bool, str | None]:
    """
    Step2a + Step2: Model-generated test validation (two sub-steps)
    
    Returns:
        tuple[bool, str]: (Whether validation passed, Generated test patch)
    """
    instance_id: str = entry["instance_id"]
    
    if not _check_step_results_exist(output_dir, "step1", instance_id):
        return False, None
    
    if (_check_step_results_exist(output_dir, "step2a", instance_id, model_name) and 
        _check_step_results_exist(output_dir, "step2", instance_id, model_name)):
        step2_path = output_dir / "step2_model" / f"{model_name.replace('/', '_').replace(':', '_')}.json"
        if step2_path.exists():
            try:
                all_step2_data = json.loads(step2_path.read_text())
                step2_data = all_step2_data.get(instance_id, {})
                step2_passed = step2_data.get("passed", False)
                generated_test_patch = step2_data.get("generated_test_patch", "")
                return step2_passed, generated_test_patch
            except (KeyError, json.JSONDecodeError, FileNotFoundError) as e:
                return False, None
        return False, None
    
    test_patches, _ = _load_test_patches()
    if instance_id not in test_patches:
        progress_manager.update_instance_status(instance_id, "No test patch found")
        return False, None
    
    instance_data = test_patches.get(instance_id, {})
    if not instance_data:
        progress_manager.update_instance_status(instance_id, "Empty test patch data")
        return False, None
    
    f2p: list[str] = [t for t in instance_data.get("F2P", []) if t]
    p2p: list[str] = [t for t in instance_data.get("P2P", []) if t]
    
    test_files = set()
    for test_node in f2p + p2p:
        if "::" in test_node:
            test_file = test_node.split("::")[0]
            test_files.add(test_file)
    test_files = list(test_files)

    if not test_files:
        test_files = instance_data.get("test_files", [])

    progress_manager.on_instance_start(instance_id)
    progress_manager.update_instance_status(instance_id, f"Step2a+2: Generated test validation ({model_name})")
    
    image_name = image_override or get_swebench_docker_image_name(instance_id)
    env = DockerEnvironment(
        image=image_name,
        timeout=timeout,
        cwd="/testbed",
        container_timeout=container_timeout
    )
    
    try:
        mock_instance = _create_mock_instance(instance_id)
        test_spec = make_test_spec(mock_instance)
        _setup_validator_env(env, test_spec, instance_id, f2p_tests=f2p)
        
        test_preds_base = os.environ.get("TEST_PREDS_BASE_PATH", "results/test_generation_new")
        test_preds_path = Path(f"{test_preds_base}/{model_name}/test_preds.json")
        test_preds = _load_test_preds(test_preds_path)
        
        if instance_id not in test_preds:
            progress_manager.update_instance_status(instance_id, f"No test prediction found for model {model_name}")
            return False, None
            
        generated_test_patch = test_preds.get(instance_id, "")
        if not generated_test_patch:
            progress_manager.update_instance_status(instance_id, f"Empty test prediction for model {model_name}")
            return False, None
        
        if not _check_step_results_exist(output_dir, "step2a", instance_id, model_name):
            progress_manager.update_instance_status(instance_id, f"Step2a: Bug detection test ({model_name})")
            
            step2a_result = _run_single_test_scenario(
                env, instance_id, test_files,
                code_patch="",
                test_patch=generated_test_patch,
                scenario_name="step2a_generated_test_bug_detection"
            )
            
            step2a_detected_bugs = not step2a_result.get("success", False) or step2a_result.get("test_results", {}).get("failed", 0) > 0
            
            step2a_data = {
                "result": step2a_result,
                "generated_test_patch": generated_test_patch,
                "detected_bugs": step2a_detected_bugs,
                "irr": 1.0 if step2a_detected_bugs else 0.0,
                "description": "Test generated test patch ability to detect original repository errors (without code patch)",
                "timestamp": time.time(),
            }
            _save_step_results(output_dir, "step2a", instance_id, step2a_data, model_name)
            
            if step2a_detected_bugs:
                progress_manager.update_instance_status(instance_id, f"Step2a: PASSED ({model_name}) - Detected bugs")
            else:
                progress_manager.update_instance_status(instance_id, f"Step2a: FAILED ({model_name}) - No bugs detected")
        
        if not _check_step_results_exist(output_dir, "step2", instance_id, model_name):
            progress_manager.update_instance_status(instance_id, f"Step2: Generated test validation ({model_name})")
            
            step2_result = _run_single_test_scenario(
                env, instance_id, test_files,
                code_patch=instance_data.get("patch", ""),
                test_patch=generated_test_patch,
                scenario_name="step2_generated_test_validation"
            )
            
            step2_passed = step2_result.get("success", False) and step2_result.get("test_results", {}).get("failed", 0) == 0
            
            step2_data = {
                "result": step2_result,
                "generated_test_patch": generated_test_patch,
                "passed": step2_passed,
                "timestamp": time.time(),
            }
            _save_step_results(output_dir, "step2", instance_id, step2_data, model_name)
            
            if step2_passed:
                progress_manager.update_instance_status(instance_id, f"Step2: PASSED ({model_name})")
                return True, generated_test_patch
            else:
                progress_manager.update_instance_status(instance_id, f"Step2: FAILED ({model_name}) - Generated test issues")
                return False, generated_test_patch
        else:
            step2_path = output_dir / "step2_model" / f"{model_name.replace('/', '_').replace(':', '_')}.json"
            if step2_path.exists():
                try:
                    all_step2_data = json.loads(step2_path.read_text())
                    step2_data = all_step2_data.get(instance_id, {})
                    step2_passed = step2_data.get("passed", False)
                    generated_test_patch = step2_data.get("generated_test_patch", instance_data.get("test_patch", ""))
                    return step2_passed, generated_test_patch
                except (KeyError, json.JSONDecodeError, FileNotFoundError) as e:
                    return False, None
            
            return False, None
    except Exception as e:
        progress_manager.update_instance_status(instance_id, f"Step2a+2: Error ({model_name}): {e}")
        error_data_step2a = {
            "error": str(e),
            "passed": False,
            "detected_bugs": False,
            "irr": 0.0,
            "timestamp": time.time(),
        }
        error_data_step2 = {
            "error": str(e),
            "passed": False,
            "timestamp": time.time(),
        }
        _save_step_results(output_dir, "step2a", instance_id, error_data_step2a, model_name)
        _save_step_results(output_dir, "step2", instance_id, error_data_step2, model_name)
        return False, None
    finally:
        env.cleanup()


def process_step4_generated_test_hack_testing(
    entry: dict,
    model_name: str,
    output_dir: Path,
    progress_manager: RunBatchProgressManager,
    timeout: int,
    image_override: str | None = None,
    container_timeout: str = "10m",
) -> bool:
    """
    Phase 4: Process Step4 - hack patches vs generated tests
    Returns:
        bool: Whether testing completed successfully (for final metrics)
    """
    instance_id: str = entry["instance_id"]
    
    if not _check_step_results_exist(output_dir, "step2", instance_id, model_name):
        print(f"[SKIP] Step4 skipped - Step2 not completed for {instance_id} with model {model_name}")
        return False
    
    if not _check_step_results_exist(output_dir, "step3", instance_id):
        print(f"[SKIP] Step4 skipped - Step3 not completed for {instance_id}")
        return False
    
    if _check_step_results_exist(output_dir, "step4", instance_id, model_name):
        print(f"[SKIP] Step4 results already exist for {instance_id} with model {model_name}")
        step4_path = output_dir / "step4_model" / f"{model_name.replace('/', '_').replace(':', '_')}.json"
        if step4_path.exists():
            try:
                all_step4_data = json.loads(step4_path.read_text())
                step4_data = all_step4_data.get(instance_id, {})
                if "metrics" in step4_data and "h_gen" in step4_data:
                    return True
                else:
                    print(f"Warning: Step4 data incomplete for {instance_id}")
                    return False
            except (KeyError, json.JSONDecodeError, FileNotFoundError) as e:
                print(f"Warning: Failed to read Step4 results for {instance_id}: {e}")
                return False
        return False
    
    print(f"\n{'='*60}")
    print(f"Processing Step4 for instance: {instance_id} with model: {model_name}")
    print(f"{'='*60}")
    
    test_patches, _ = _load_test_patches()
    hack_data = _load_hack_patches()
    if instance_id not in test_patches or instance_id not in hack_data:
        progress_manager.update_instance_status(instance_id, "No test data found")
        return False
    
    instance_data = test_patches.get(instance_id, {})
    hack_patches = hack_data.get(instance_id, [])
    
    if not instance_data or not hack_patches:
        progress_manager.update_instance_status(instance_id, f"Empty data - instance_data: {bool(instance_data)}, hack_patches: {bool(hack_patches)}")
        return False
    
    f2p: list[str] = [t for t in instance_data.get("F2P", []) if t]
    p2p: list[str] = [t for t in instance_data.get("P2P", []) if t]
    
    test_files = set()
    for test_node in f2p + p2p:
        if "::" in test_node:
            test_file = test_node.split("::")[0]
            test_files.add(test_file)
    test_files = list(test_files)

    if not test_files:
        test_files = instance_data.get("test_files", [])

    progress_manager.update_instance_status(instance_id, f"Step4: Generated test hack testing ({model_name})")
    
    image_name = image_override or get_swebench_docker_image_name(instance_id)
    env = DockerEnvironment(
        image=image_name,
        timeout=timeout,
        cwd="/testbed",
        container_timeout=container_timeout
    )
    
    try:
        mock_instance = _create_mock_instance(instance_id)
        test_spec = make_test_spec(mock_instance)
        _setup_validator_env(env, test_spec, instance_id, f2p_tests=f2p)
        
        step2_path = output_dir / "step2_model" / f"{model_name.replace('/', '_').replace(':', '_')}.json"
        if step2_path.exists():
            try:
                all_step2_data = json.loads(step2_path.read_text())
                step2_data = all_step2_data.get(instance_id, {})
                generated_test_patch = step2_data.get("generated_test_patch", instance_data.get("test_patch", ""))
            except (KeyError, json.JSONDecodeError, FileNotFoundError) as e:
                print(f"Warning: Failed to read Step2 generated_test_patch for {instance_id}: {e}")
                generated_test_patch = instance_data.get("test_patch", "")
        else:
            generated_test_patch = instance_data.get("test_patch", "")  # fallback
        
        step4_results = {}
        for hack_idx, hack_patch in enumerate(hack_patches, 1):
            hack_id = f"hack_{hack_idx}"
            
            test_files_to_exclude = instance_data.get("test_files", [])
            filtered_hack_patch = _exclude_files_from_diff(hack_patch, test_files_to_exclude)
            
            if not filtered_hack_patch.strip():
                continue
            
            hack_result = _run_single_test_scenario(
                env, instance_id, test_files,
                code_patch=filtered_hack_patch,
                test_patch=generated_test_patch,
                scenario_name=f"step4_hack_{hack_id}"
            )
            step4_results[hack_id] = hack_result
        
        h_gen = set()
        for hack_id, result in step4_results.items():
            if not result.get("success", False) or result.get("test_results", {}).get("failed", 0) > 0:
                h_gen.add(hack_id)
        
        step3_path = output_dir / "step3.json"
        h_base = set()
        if step3_path.exists():
            try:
                all_step3_data = json.loads(step3_path.read_text())
                step3_data = all_step3_data.get(instance_id, {})
                h_base = set(step3_data.get("h_base", []))
            except (KeyError, json.JSONDecodeError, FileNotFoundError) as e:
                print(f"Warning: Failed to read h_base for {instance_id}: {e}")
                h_base = set()
        
        h_total = set(f"hack_{i}" for i in range(1, len(hack_patches) + 1))
        h_hidden = h_total - h_base
        h_gen_hidden = h_gen - h_base
        h_gen_base = h_gen & h_base
        
        rdr = len(h_gen_hidden) / len(h_hidden) if len(h_hidden) > 0 else 0.0
        
        step4_data = {
            "hack_results": step4_results,
            "h_gen": list(h_gen),
            "metrics": {
                "rdr": rdr,
                "h_total_count": len(h_total),
                "h_base_count": len(h_base),
                "h_gen_count": len(h_gen),
                "h_hidden_count": len(h_hidden),
                "h_gen_hidden_count": len(h_gen_hidden),
                "h_gen_base_count": len(h_gen_base),
                "h_base_list": list(h_base),
                "h_gen_list": list(h_gen),
                "h_gen_hidden_list": list(h_gen_hidden),
                "h_gen_base_list": list(h_gen_base),
            },
            "timestamp": time.time(),
        }
        _save_step_results(output_dir, "step4", instance_id, step4_data, model_name)
        
        progress_manager.update_instance_status(instance_id, f"Step4: COMPLETED ({model_name}) RDR={rdr:.3f}")
        return True
        
    except Exception as e:
        print(f"Error processing Step4 for instance {instance_id} with model {model_name}: {e}")
        progress_manager.update_instance_status(instance_id, f"Step4: Error ({model_name}): {e}")
        error_data = {
            "error": str(e),
            "timestamp": time.time(),
        }
        _save_step_results(output_dir, "step4", instance_id, error_data, model_name)
        return False
    finally:
        env.cleanup()


def process_coverage_calculation(
    entry: dict,
    model_name: str,
    output_dir: Path,
    progress_manager: RunBatchProgressManager,
    timeout: int,
    image_override: str | None = None,
    container_timeout: str = "10m",
) -> bool:
    """
    Standalone code coverage calculation step:
    1. Setup container environment
    2. Apply model-generated test patch
    3. Run tests and calculate coverage
    4. Save coverage results
    Returns:
        bool: Whether coverage calculation succeeded
    """
    instance_id: str = entry["instance_id"]
    
    if _check_step_results_exist(output_dir, "coverage", instance_id, model_name):
        print(f"[SKIP] Coverage results already exist for {instance_id} with model {model_name}")
        return True
    
    print(f"\n{'='*60}")
    print(f"Processing Coverage Calculation for instance: {instance_id} with model: {model_name}")
    print(f"{'='*60}")
    
    test_patches, _ = _load_test_patches()
    if instance_id not in test_patches:
        progress_manager.update_instance_status(instance_id, "No test patch found")
        return False
    
    instance_data = test_patches.get(instance_id, {})
    if not instance_data:
        progress_manager.update_instance_status(instance_id, "Empty test patch data")
        return False
    
    f2p: list[str] = [t for t in instance_data.get("F2P", []) if t]
    p2p: list[str] = [t for t in instance_data.get("P2P", []) if t]
    
    test_files = set()
    for test_node in f2p + p2p:
        if "::" in test_node:
            test_file = test_node.split("::")[0]
            test_files.add(test_file)
    test_files = list(test_files)

    if not test_files:
        test_files = instance_data.get("test_files", [])

    progress_manager.on_instance_start(instance_id)
    progress_manager.update_instance_status(instance_id, f"Coverage: Calculating test coverage ({model_name})")
    
    image_name = image_override or get_swebench_docker_image_name(instance_id)
    env = DockerEnvironment(
        image=image_name,
        timeout=timeout,
        cwd="/testbed",
        container_timeout=container_timeout
    )
    
    try:
        mock_instance = _create_mock_instance(instance_id)
        test_spec = make_test_spec(mock_instance)
        _setup_validator_env(env, test_spec, instance_id, f2p_tests=f2p)
        
        test_preds_base = os.environ.get("TEST_PREDS_BASE_PATH", "results/test_generation_new")
        test_preds_path = Path(f"{test_preds_base}/{model_name}/test_preds.json")
        test_preds = _load_test_preds(test_preds_path)
        
        if instance_id not in test_preds:
            progress_manager.update_instance_status(instance_id, f"No test prediction found for model {model_name}")
            return False
            
        generated_test_patch = test_preds.get(instance_id, "")
        if not generated_test_patch:
            progress_manager.update_instance_status(instance_id, f"Empty test prediction for model {model_name}")
            return False
        
        _git_reset_clean(env)
        
        if instance_data.get("patch"):
            code_apply_result = _write_and_apply_patch(env, instance_data["patch"], "coverage_code")
            progress_manager.update_instance_status(instance_id, f"Coverage: Applied code patch - {code_apply_result}")
        
        test_apply_result = _write_and_apply_patch(env, generated_test_patch, "coverage_test")
        progress_manager.update_instance_status(instance_id, f"Coverage: Applied test patch - {test_apply_result}")
        
        progress_manager.update_instance_status(instance_id, f"Coverage: Running coverage tests...")
        coverage_result = _run_coverage_tests(env, test_files, instance_id)
        
        coverage_success = coverage_result.get("success", False)
        coverage_data = coverage_result.get("coverage", {})
        total_coverage = coverage_data.get("total_coverage", 0.0)
        
        coverage_step_data = {
            "coverage_result": coverage_result,
            "generated_test_patch": generated_test_patch,
            "success": coverage_success,
            "total_coverage": total_coverage,
            "lines_covered": coverage_data.get("lines_covered", 0),
            "lines_total": coverage_data.get("lines_total", 0),
            "file_coverage": coverage_data.get("file_coverage", {}),
            "coverage_summary": coverage_data.get("coverage_summary", ""),
            "test_files": test_files,
            "commands_tried": coverage_result.get("commands_tried", 0),
            "source_dirs": coverage_result.get("source_dirs", []),
            "timestamp": time.time(),
        }
        _save_step_results(output_dir, "coverage", instance_id, coverage_step_data, model_name)
        
        if coverage_success:
            progress_manager.update_instance_status(
                instance_id, 
                f"Coverage: COMPLETED ({model_name}) - {total_coverage:.1f}% coverage"
            )
        else:
            progress_manager.update_instance_status(
                instance_id, 
                f"Coverage: FAILED ({model_name}) - Unable to calculate coverage"
            )
        
        return coverage_success
        
    except Exception as e:
        print(f"Error processing coverage calculation for instance {instance_id} with model {model_name}: {e}")
        progress_manager.update_instance_status(instance_id, f"Coverage: Error ({model_name}): {e}")
        error_data = {
            "error": str(e),
            "success": False,
            "total_coverage": 0.0,
            "timestamp": time.time(),
        }
        _save_step_results(output_dir, "coverage", instance_id, error_data, model_name)
        return False
    finally:
        env.cleanup()


def _create_mock_instance(instance_id: str) -> dict:
    """Helper to create a mock instance, reusing version mapping logic"""
    mock_instance = {"instance_id": instance_id}
    
    for line in TEST_PATCHES_PATH.read_text().splitlines():
        if line.strip():
            data = json.loads(line)
            data_instance_id = data.get("instance_id", data.get("id", data.get("name")))
            if data_instance_id == instance_id:
                mock_instance.update({
                    "repo": data.get("repo", ""),
                    "base_commit": data.get("base_commit", "main"),
                    "patch": data.get("patch", ""),
                    "test_patch": data.get("test_patch", ""),
                    "problem_statement": data.get("problem_statement", ""),
                    "hints_text": data.get("hints_text", ""),
                    "version": data.get("version", ""),
                    "FAIL_TO_PASS": data.get("FAIL_TO_PASS", []),
                    "PASS_TO_PASS": data.get("PASS_TO_PASS", []),
                })
                break
    
    if not mock_instance.get("repo"):
        if "__" in instance_id:
            parts = instance_id.split("__")
            if len(parts) >= 2:
                org = parts[0]
                project_version = parts[1]
                if "-" in project_version:
                    project = project_version.rsplit("-", 1)[0]
                    version = project_version.rsplit("-", 1)[1]
                else:
                    project = project_version
                    version = ""
                
                repo = f"{org}/{project}"
                
                if repo == "astropy/astropy" and version.isdigit():
                    version = "5.0"
                elif repo == "django/django" and version.isdigit():
                    version_num = int(version)
                    if version_num < 10000:
                        version = "1.11"
                    elif version_num < 15000:
                        version = "3.2"
                    elif version_num < 20000:
                        version = "4.2"
                    else:
                        version = "5.0"
                elif repo == "matplotlib/matplotlib" and version.isdigit():
                    version_num = int(version)
                    if version_num < 20000:
                        version = "3.0"
                    elif version_num < 25000:
                        version = "3.5"
                    else:
                        version = "3.7"
                elif repo == "pydata/xarray" and version.isdigit():
                    version_num = int(version)
                    if version_num < 5000:
                        version = "0.12"
                    elif version_num < 6500:
                        version = "0.19"
                    else:
                        version = "2022.03"
                elif repo == "scikit-learn/scikit-learn" and version.isdigit():
                    version_num = int(version)
                    if version_num < 10000:
                        version = "0.20"
                    elif version_num < 13000:
                        version = "0.22"
                    else:
                        version = "1.3"
                elif repo == "sympy/sympy" and version.isdigit():
                    version_num = int(version)
                    if version_num < 15000:
                        version = "1.1"
                    elif version_num < 20000:
                        version = "1.5"
                    else:
                        version = "1.9"
                elif repo == "sphinx-doc/sphinx" and version.isdigit():
                    version_num = int(version)
                    if version_num < 5000:
                        version = "1.8"
                    elif version_num < 7500:
                        version = "3.0"
                    else:
                        version = "4.0"
                
                mock_instance.update({
                    "repo": repo,
                    "version": version,
                    "base_commit": "main",
                    "patch": "",
                    "test_patch": "",
                    "problem_statement": "",
                    "hints_text": "",
                    "FAIL_TO_PASS": [],
                    "PASS_TO_PASS": [],
                })
    
    return mock_instance


if __name__ == "__main__":
    app()
