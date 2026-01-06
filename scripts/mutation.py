#!/usr/bin/env python3

import concurrent.futures
import json
import os
import random
import re
import shlex
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

import typer
import yaml
from datasets import load_dataset
from rich.live import Live

SWE_BENCH_PATH = os.environ.get("SWE_BENCH_PATH", "")
if SWE_BENCH_PATH:
    sys.path.append(SWE_BENCH_PATH)

try:
    from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
    from swebench.harness.test_spec.test_spec import make_test_spec
    from swebench.harness.log_parsers import MAP_REPO_TO_PARSER
except ImportError as e:
    print(f"Error importing swebench modules: {e}")
    sys.exit(1)

from minisweagent.agents.default import (
    DefaultAgent,
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
TEST_PATCHES_PATH = Path(os.environ.get("TEST_PATCHES_PATH", "test_patches.jsonl"))


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


def _get_completed_strategy_groups(output_path: Path, instance_id: str) -> set[str]:
    if not output_path.exists():
        return set()
    
    try:
        content = output_path.read_text()
        if not content.strip():
            return set()
        
        output_data = json.loads(content)
        if instance_id not in output_data:
            return set()
        
        instance_data = output_data[instance_id]
        model_patch = instance_data.get("model_patch", "")
        
        if isinstance(model_patch, str) and model_patch.strip():
            try:
                patch_data = json.loads(model_patch)
            except (json.JSONDecodeError, ValueError):
                return set()
        elif isinstance(model_patch, dict):
            patch_data = model_patch
        else:
            return set()
        
        if "completed_groups" in patch_data:
            return set(patch_data["completed_groups"])
        
        hacks = patch_data.get("hacks", [])
        completed_groups = set()
        for hack in hacks:
            group_code = hack.get("strategy_group", "")
            if group_code:
                completed_groups.add(group_code)
        
        return completed_groups
    
    except (json.JSONDecodeError, ValueError, KeyError):
        return set()


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
        obj = json.loads(line)
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
            "repo_description": obj.get("repo_description", obj.get("problem_statement", "")),
        }
    return mapping


def _git_reset_clean(env: DockerEnvironment):
    env.execute("git reset --hard && git clean -fd && git checkout .")


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


def _run_build_commands_from_test_spec(env: DockerEnvironment, test_spec, stage: str, instance_id: str = "") -> None:
    try:
        repo = getattr(test_spec, 'repo', '')
        version = getattr(test_spec, 'version', '')
        
        if not repo or not version:
            return
        
        check_compiled = env.execute("find . -name '*.class' -type f | head -1")
        has_compiled_classes = bool(check_compiled.get("output", "").strip())
        
        if has_compiled_classes:
            check_modified = env.execute("""
                find . -name '*.java' -newer $(find . -name '*.class' | head -1) 2>/dev/null | head -5
            """)
            
            modified_sources = check_modified.get("output", "").strip()
            if not modified_sources:
                return
        
        from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
        specs = MAP_REPO_VERSION_TO_SPECS.get(repo, {}).get(version, {})
        
        build_commands = specs.get("build", [])
        if isinstance(build_commands, str):
            build_commands = [build_commands]
        
        if build_commands:
            for build_cmd in build_commands:
                if build_cmd:
                    if "mvn" in build_cmd:
                        optimized_cmd = build_cmd.replace("mvn ", "mvn -T 4 -Dmaven.javadoc.skip=true -Dmaven.test.skip=true ")
                        optimized_cmd = f"timeout 300 {optimized_cmd}"
                        build_cmd = optimized_cmd
                    
                    result = env.execute(f"cd /testbed && {build_cmd}")
                    
                    if result.get("returncode", 1) not in [0, 124]:
                        if result.get("returncode") == 124:
                            pass
    except Exception:
        pass


def _setup_validator_env(env: DockerEnvironment, test_spec, instance_id: str = "", f2p_tests: list[str] = None) -> None:
    proxy_url = os.environ.get("PROXY_URL", "")
    if proxy_url:
        env_setup = f"""
export http_proxy={proxy_url}
export https_proxy={proxy_url}
export HTTP_PROXY={proxy_url}
export HTTPS_PROXY={proxy_url}
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
            
            language = getattr(test_spec, 'language', 'unknown')
            
            if language == 'py':
                setup_cmd = """
cd /testbed
if [ -d "/opt/miniconda3" ]; then
    source /opt/miniconda3/etc/profile.d/conda.sh
    bash /tmp/setup_env.sh
else
    bash /tmp/setup_env.sh
fi
"""
            elif language == 'js':
                setup_cmd = """
cd /testbed
if [ -d "/usr/local/nvm" ]; then
    export NVM_DIR="/usr/local/nvm"
    [ -s "$NVM_DIR/nvm.sh" ] && source "$NVM_DIR/nvm.sh"
fi
bash /tmp/setup_env.sh
"""
            elif language == 'java':
                setup_cmd = """
cd /testbed
export JAVA_HOME=${JAVA_HOME:-/usr/lib/jvm/default-java}
export MAVEN_HOME=${MAVEN_HOME:-/usr/share/maven}
export PATH="$MAVEN_HOME/bin:$PATH"
bash /tmp/setup_env.sh
"""
            elif language == 'go':
                setup_cmd = """
cd /testbed
export GOROOT=${GOROOT:-/usr/local/go}
export PATH="$GOROOT/bin:$PATH"
bash /tmp/setup_env.sh
"""
            elif language in ['rs', 'rust']:
                setup_cmd = """
cd /testbed
if [ -f "$HOME/.cargo/env" ]; then
    source "$HOME/.cargo/env"
fi
bash /tmp/setup_env.sh
"""
            else:
                setup_cmd = """
cd /testbed
bash /tmp/setup_env.sh
"""
            
            result = env.execute(setup_cmd)
            
            if result.get("returncode", 1) != 0:
                pass
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
                        result = env.execute(f"cd /testbed && {cmd}")
    except Exception:
        pass
    
    try:
        repo = getattr(test_spec, 'repo', '')
        version = getattr(test_spec, 'version', '')
        pre_install_executed = False
        
        if repo and version:
            try:
                from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
                specs = MAP_REPO_VERSION_TO_SPECS.get(repo, {}).get(version, {})
                pre_install = specs.get("pre_install", None)
                
                if pre_install:
                    if repo == "projectlombok/lombok" and f2p_tests:
                        tests_xml = "\n        ".join(f'<test name="{test}" />' for test in f2p_tests)
                        target_xml = f"""    <target name="test.instance" depends="test.compile, test.formatter.compile" description="Runs test cases for the swe-bench instance">
      <junit printsummary="yes" fork="true" forkmode="once" haltonfailure="no">
        <formatter classname="lombok.ant.SimpleTestFormatter" usefile="false" unless="tests.quiet" />
        <classpath location="build/ant" />
        <classpath refid="cp.test" />
        <classpath refid="cp.stripe" />
        <classpath refid="packing.basedirs.path" />
        <classpath location="build/tests" />
        <classpath location="build/teststubs" />
        {tests_xml}
      </junit>
    </target>"""
                        
                        xml_marker = f"LOMBOK_XML_EOF_{uuid.uuid4().hex[:8]}"
                        write_xml_cmd = f"cat > /tmp/test_instance_target.xml << '{xml_marker}'\n{target_xml}\n{xml_marker}"
                        env.execute(write_xml_cmd)
                        
                        build_file = "/testbed/buildScripts/tests.ant.xml"
                        modify_cmd = f"""
cd /testbed
cp {build_file} {build_file}.bak
head -n -1 {build_file}.bak > {build_file}
cat /tmp/test_instance_target.xml >> {build_file}
echo '</project>' >> {build_file}
if grep -q 'test.instance' {build_file}; then
    echo 'Successfully added test.instance target'
    exit 0
else
    echo 'Failed to add test.instance target, restoring backup'
    mv {build_file}.bak {build_file}
    exit 1
fi
"""
                        
                        result = env.execute(modify_cmd)
                        if result.get("returncode") == 0:
                            pre_install_executed = True
                            
                    elif callable(pre_install):
                        if f2p_tests:
                            pre_install_commands = pre_install(f2p_tests)
                        else:
                            pre_install_commands = pre_install([])
                        
                        for cmd in pre_install_commands:
                            if cmd.strip():
                                result = env.execute(f"cd /testbed && {cmd}")
                                if result.get("returncode", 1) == 0:
                                    pre_install_executed = True
                    elif isinstance(pre_install, list):
                        for cmd in pre_install:
                            if cmd.strip():
                                result = env.execute(f"cd /testbed && {cmd}")
                                if result.get("returncode", 1) == 0:
                                    pre_install_executed = True
            except Exception:
                pass
        
        if not pre_install_executed:
            if hasattr(test_spec, 'pre_install_commands') and test_spec.pre_install_commands:
                for cmd in test_spec.pre_install_commands:
                    if cmd.strip():
                        result = env.execute(f"cd /testbed && {cmd}")
    except Exception:
        pass


def _find_maven_test_result(lines: list[str], test_name: str) -> dict:
    for i, line in enumerate(lines):
        if test_name in line and "-Dtest=" in line:
            for j in range(i, min(i + 50, len(lines))):
                result_line = lines[j].strip()
                if "Tests run:" in result_line:
                    match = re.search(r'Tests run:\s*(\d+)', result_line)
                    if match:
                        total_tests = int(match.group(1))
                        failures_match = re.search(r'Failures:\s*(\d+)', result_line)
                        errors_match = re.search(r'Errors:\s*(\d+)', result_line)
                        
                        failed = int(failures_match.group(1)) if failures_match else 0
                        errors = int(errors_match.group(1)) if errors_match else 0
                        passed = total_tests - failed - errors
                        
                        return {
                            "passed": passed,
                            "failed": failed,
                            "errors": errors,
                            "skipped": 0,
                            "summary": result_line
                        }
    return None


def _parse_test_output_with_swe_bench(output: str, test_cmd: str = "", test_nodeids: list[str] = None, instance_id: str = "") -> dict:
    passed = failed = errors = skipped = 0
    summary = ""
    
    try:
        if "__" in instance_id:
            org_project = instance_id.split("__")[1].split("-")[0]
            org = instance_id.split("__")[0]
            
            if org == "apache":
                repo_name = f"apache/{org_project}"
            elif org == "astral-sh":
                repo_name = f"astral-sh/{org_project}"
            elif org == "google":
                repo_name = f"google/{org_project}"
            else:
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
        
        f2p_failed_count = 0
        f2p_passed_count = 0
        
        if test_nodeids:
            for test_node in test_nodeids:
                test_status = None
                
                if test_node in test_status_map:
                    test_status = test_status_map[test_node]
                else:
                    for parsed_test, status in test_status_map.items():
                        if test_node in parsed_test or parsed_test in test_node:
                            test_status = status
                            break
                    
                    if test_status is None:
                        test_status = "PASSED"
                
                if test_status in ["FAILED", "FAIL", "ERROR", "ERR"]:
                    f2p_failed_count += 1
                    failed += 1
                elif test_status in ["PASSED", "PASS"]:
                    f2p_passed_count += 1
                    passed += 1
                elif test_status in ["SKIPPED", "SKIP"]:
                    skipped += 1
        
        total_passed = sum(1 for status in test_status_map.values() if status in ["PASSED", "PASS"])
        total_failed = sum(1 for status in test_status_map.values() if status in ["FAILED", "FAIL", "ERROR", "ERR"])
        total_skipped = sum(1 for status in test_status_map.values() if status in ["SKIPPED", "SKIP"])
        
        total = total_passed + total_failed + total_skipped
        summary = f"Parsed {total} tests: {total_passed} passed, {total_failed} failed, {total_skipped} skipped"
        
        result = {
            "passed": f2p_passed_count,
            "failed": f2p_failed_count,
            "errors": 0,
            "skipped": skipped,
            "total": f2p_passed_count + f2p_failed_count + skipped,
            "summary": f"F2P tests: {f2p_passed_count} passed, {f2p_failed_count} failed; {summary}",
            "all_test_status": test_status_map
        }
        
        return result
        
    except Exception:
        return _parse_test_output(output, test_cmd, test_nodeids)


def _parse_test_output(output: str, test_cmd: str = "", requested_tests: list[str] = None) -> dict:
    passed = failed = errors = skipped = 0
    summary = ""
    
    try:
        lines = output.split('\n')
        
        if requested_tests and "mvn" in test_cmd.lower():
            for test_name in requested_tests:
                test_result = _find_maven_test_result(lines, test_name)
                if test_result:
                    passed += test_result.get("passed", 0)
                    failed += test_result.get("failed", 0)
                    errors += test_result.get("errors", 0)
                    skipped += test_result.get("skipped", 0)
                    if summary:
                        summary += "; "
                    summary += test_result.get("summary", "")
            
            if passed + failed + errors + skipped > 0:
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
        
        for idx, line in enumerate(reversed(lines)):
            line_stripped = line.strip()
            if not line_stripped:
                continue
            
            if "forbidden API" in line_stripped or "Scanned" in line_stripped:
                continue
            
            if line_stripped.startswith("Tests:") and "Assertions:" in line_stripped:
                tests_match = re.search(r'Tests:\s*(\d+)', line_stripped)
                failures_match = re.search(r'Failures:\s*(\d+)', line_stripped)
                errors_match = re.search(r'Errors:\s*(\d+)', line_stripped)
                
                if tests_match:
                    total_tests = int(tests_match.group(1))
                    failed = int(failures_match.group(1)) if failures_match else 0
                    errors = int(errors_match.group(1)) if errors_match else 0
                    passed = total_tests - failed - errors
                    summary = line_stripped
                    break
            
            if line_stripped.startswith("# tests"):
                match = re.search(r'#\s*tests\s+(\d+)', line_stripped)
                if match:
                    total_tests = int(match.group(1))
                    for i in range(max(0, len(lines) - idx - 5), min(len(lines), len(lines) - idx + 5)):
                        check_line = lines[i].strip()
                        pass_match = re.search(r'#\s*pass\s+(\d+)', check_line)
                        fail_match = re.search(r'#\s*fail\s+(\d+)', check_line)
                        if pass_match:
                            passed = int(pass_match.group(1))
                        if fail_match:
                            failed = int(fail_match.group(1))
                    summary = f"TAP: {total_tests} tests, {passed} passed, {failed} failed"
                    break
            
            if "Tests run:" in line_stripped:
                match = re.search(r'Tests run:\s*(\d+)', line_stripped)
                if match:
                    total_tests = int(match.group(1))
                    failures_match = re.search(r'Failures:\s*(\d+)', line_stripped)
                    errors_match = re.search(r'Errors:\s*(\d+)', line_stripped)
                    
                    failed = int(failures_match.group(1)) if failures_match else 0
                    errors = int(errors_match.group(1)) if errors_match else 0
                    passed = total_tests - failed - errors
                    summary = line_stripped
                    break
            
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
            
            if "passed" in line_stripped.lower() and ("failed" in line_stripped.lower() or "error" in line_stripped.lower()):
                numbers = re.findall(r'(\d+)', line_stripped)
                if numbers:
                    summary = line_stripped
                    break
    
    except Exception:
        pass
    
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


def _convert_django_test_name(test_name: str) -> str:
    match = re.match(r'^(\S+)\s+\(([^)]+)\)$', test_name)
    if match:
        test_method = match.group(1)
        test_class = match.group(2)
        return f"{test_class}.{test_method}"
    return test_name


def _is_django_instance(instance_id: str, test_cmd: str = "") -> bool:
    return "django" in instance_id.lower() or "runtests.py" in test_cmd


def _run_tests_with_cmd(env: DockerEnvironment, test_cmd: str, test_nodeids: list[str] = None, instance_id: str = "") -> tuple[bool, dict]:
    if not test_cmd:
        return True, {"test_results": {}, "output": ""}
    
    cmd = test_cmd
    is_chained_redirection = '&&' in cmd and '>' in cmd and 'cat' in cmd
    
    if "runtests.py" in cmd or "django" in instance_id.lower():
        cmd = f"export PYTHONIOENCODING=utf-8 && export LC_ALL=C.UTF-8 && export LANG=C.UTF-8 && {cmd}"
    
    if "pytest" in cmd:
        cmd = f"export PYTHONWARNINGS='ignore::DeprecationWarning' && {cmd}"
    
    test_timeout = 900
    
    if re.match(r'^\w+=[^\s]+(\s+\w+=[^\s]+)*\s+', cmd):
        cmd = f"timeout {test_timeout} bash -c {shlex.quote(cmd)}"
    else:
        cmd = f"timeout {test_timeout} {cmd}"
    
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


def _parse_jest_test_status(output: str, test_names: list[str]) -> dict:
    test_status = {}
    lines = output.split('\n')
    
    for test_name in test_names:
        found = False
        for line in lines:
            if test_name.lower() in line.lower():
                if '✓' in line or 'PASS' in line:
                    test_status[test_name] = "passed"
                    found = True
                    break
                elif '✕' in line or 'FAIL' in line or '×' in line:
                    test_status[test_name] = "failed"
                    found = True
                    break
                elif '○' in line or 'skipped' in line.lower():
                    test_status[test_name] = "skipped"
                    found = True
                    break
        
        if not found:
            test_status[test_name] = "unknown"
    
    return test_status


def _run_pytest_tests(env: DockerEnvironment, test_nodeids: list[str], instance_id: str = "") -> tuple[bool, dict]:
    if not test_nodeids:
        return True, {"pytest_results": {}, "output": ""}
    
    cmd = f"python -m pytest -xvs {' '.join(test_nodeids)}"
    res = env.execute(f"{cmd} 2>&1 | cat")
    output = res.get("output", "")
    returncode = res.get("returncode", 1)
    
    pytest_results = _parse_test_output(output, cmd)
    
    return returncode == 0, {
        "pytest_results": pytest_results,
        "output": output
    }


def _match_test_cmd_for_tests(test_cmd_list: list, test_names: list[str]) -> str:
    if not test_cmd_list or not test_names:
        return ""
    
    if len(test_cmd_list) > 1:
        first_cmd = test_cmd_list[0]
        if '>' in first_cmd and ('/tmp/' in first_cmd or '/var/tmp/' in first_cmd):
            for subsequent_cmd in test_cmd_list[1:]:
                if 'cat' in subsequent_cmd or 'echo' in subsequent_cmd:
                    merged_cmd = ' && '.join(test_cmd_list)
                    return merged_cmd
    
    for test_name in test_names:
        for cmd in test_cmd_list:
            if test_name in cmd:
                return cmd
    
    return test_cmd_list[0]


def _verify_candidate(
    env: DockerEnvironment,
    test_spec,
    instance: dict,
    f2p_tests: list[str] = None,
    p2p_tests: list[str] = None,
    test_cmd: str = None
) -> dict:
    instance_id = instance.get("instance_id", "")
    
    if f2p_tests is None:
        f2p_tests = instance.get("FAIL_TO_PASS", []) or []
    if p2p_tests is None:
        p2p_tests = instance.get("PASS_TO_PASS", []) or []
    
    test_cmd_list = None
    
    if isinstance(test_cmd, list):
        test_cmd_list = test_cmd
        test_cmd = ""
    
    if test_cmd is None or test_cmd == "":
        try:
            repo = instance.get("repo", "")
            version = instance.get("version", "")
            if repo and version:
                specs = MAP_REPO_VERSION_TO_SPECS.get(repo, {}).get(version, {})
                fetched_test_cmd = specs.get("test_cmd", "")
                if isinstance(fetched_test_cmd, list):
                    test_cmd_list = fetched_test_cmd
                    test_cmd = ""
                else:
                    test_cmd = fetched_test_cmd
        except Exception:
            test_cmd = ""
    
    f2p_info = {"test_results": {}, "output": ""}
    p2p_info = {"test_results": {}, "output": ""}
    
    run_test_func = _run_tests_with_cmd if test_cmd else _run_pytest_tests
    
    can_select_tests = bool(test_cmd or (f2p_tests and p2p_tests))
    
    p2p_info = {}
    p2p_results = {}
    
    if can_select_tests:
        pass
    else:
        if p2p_tests:
            if test_cmd:
                _, p2p_info = _run_tests_with_cmd(env, test_cmd, p2p_tests, instance_id)
            else:
                _, p2p_info = _run_pytest_tests(env, p2p_tests, instance_id)
            
            p2p_results = p2p_info.get("test_results", p2p_info.get("pytest_results", {}))
    
    if f2p_tests:
        f2p_test_cmd = test_cmd
        if test_cmd_list:
            f2p_test_cmd = _match_test_cmd_for_tests(test_cmd_list, f2p_tests)
        
        if f2p_test_cmd:
            _, f2p_info = _run_tests_with_cmd(env, f2p_test_cmd, f2p_tests, instance_id)
        else:
            _, f2p_info = _run_pytest_tests(env, f2p_tests, instance_id)
        
        f2p_results = f2p_info.get("test_results", f2p_info.get("pytest_results", {}))
        
        f2p_failed = False
        failed_tests = []
        passed_tests = []
        
        if test_cmd and ("jest" in test_cmd or "yarn jest" in test_cmd):
            output = f2p_info.get("output", "")
            test_status = _parse_jest_test_status(output, f2p_tests)
            
            for test_name, status in test_status.items():
                if status == "failed":
                    f2p_failed = True
                    failed_tests.append(test_name)
                elif status == "passed":
                    passed_tests.append(test_name)
                elif status == "unknown":
                    if f2p_results.get("failed", 0) > 0 or f2p_results.get("errors", 0) > 0:
                        f2p_failed = True
                        failed_tests.append(test_name)
                    else:
                        passed_tests.append(test_name)
        else:
            f2p_failed = (f2p_results.get("failed", 0) > 0) or (f2p_results.get("errors", 0) > 0)
            if f2p_failed:
                failed_tests = f2p_tests
            else:
                passed_tests = f2p_tests
        
        if f2p_failed:
            result = {
                "ok": True,
                "reason": "f2p_failed",
                "f2p_results": {"pass": passed_tests, "fail": failed_tests},
                "p2p_results": p2p_results,
                "output_summary": f2p_info.get("output", "")[:1000]
            }
            return result
        else:
            result = {
                "ok": False,
                "reason": "no_f2p_failure",
                "f2p_results": {"pass": passed_tests, "fail": failed_tests},
                "p2p_results": p2p_results,
                "output_summary": f2p_info.get("output", "")[:1000]
            }
            return result
    else:
        return {
            "ok": False,
            "reason": "no_f2p_tests",
            "f2p_results": {"pass": [], "fail": []},
            "p2p_results": p2p_results,
            "output_summary": ""
        }


class InjectorAgent(DefaultAgent):
    def run_once(self, task: str, *, template_vars: dict | None = None) -> tuple[str, str]:
        self.messages = []
        self.add_message("system", self.render_template(self.config.system_template))
        self.add_message("user", self.render_template(self.config.instance_template, task=task, **(template_vars or {})))
        while True:
            try:
                self.step()
            except NonTerminatingException as e:
                self.add_message("user", str(e))
            except TerminatingException as e:
                self.add_message("user", str(e))
                return type(e).__name__, str(e)

    def continue_with_feedback(self, feedback: str) -> tuple[str, str]:
        self.add_message("user", feedback)
        while True:
            try:
                self.step()
            except NonTerminatingException as e:
                self.add_message("user", str(e))
            except TerminatingException as e:
                self.add_message("user", str(e))
                return type(e).__name__, str(e)


class ProgressTrackingInjectorAgent(DefaultAgent):
    def __init__(self, *args, progress_manager: RunBatchProgressManager, instance_id: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.progress_manager: RunBatchProgressManager = progress_manager
        self.instance_id = instance_id

    def step(self) -> dict:
        self.progress_manager.update_instance_status(
            self.instance_id, f"Step {self.model.n_calls + 1:3d} (${self.model.cost:.2f})"
        )
        return super().step()

    def run_once(self, task: str, *, template_vars: dict | None = None) -> tuple[str, str]:
        self.messages = []
        self.add_message("system", self.render_template(self.config.system_template))
        self.add_message("user", self.render_template(self.config.instance_template, task=task, **(template_vars or {})))
        while True:
            try:
                self.step()
            except NonTerminatingException as e:
                self.add_message("user", str(e))
            except TerminatingException as e:
                self.add_message("user", str(e))
                return type(e).__name__, str(e)

    def continue_with_feedback(self, feedback: str) -> tuple[str, str]:
        self.add_message("user", feedback)
        while True:
            try:
                self.step()
            except NonTerminatingException as e:
                self.add_message("user", str(e))
            except TerminatingException as e:
                self.add_message("user", str(e))
                return type(e).__name__, str(e)


def _write_ipc(host_dir: Path, name: str, data: dict):
    (host_dir / name).write_text(json.dumps(data, ensure_ascii=False, indent=2))


def _read_ipc(host_dir: Path, name: str) -> dict:
    p = host_dir / name
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _start_env(image: str, cwd: str, host_ipc: Path, container_ipc: str, timeout: int = 1800, instance_id: str = "") -> DockerEnvironment:
    host_ipc.mkdir(parents=True, exist_ok=True)
    abs_host_ipc = host_ipc.resolve()
    proxy_url = os.environ.get("PROXY_URL", "")
    proxy_host = os.environ.get("PROXY_HOST", "")
    proxy_port = os.environ.get("PROXY_PORT", "")
    
    run_args = [
        "-v", f"{abs_host_ipc}:{container_ipc}",
        "--privileged",
        "--user", "root",
    ]
    
    if proxy_url:
        run_args.extend([
            "-e", f"http_proxy={proxy_url}",
            "-e", f"https_proxy={proxy_url}",
            "-e", f"HTTP_PROXY={proxy_url}",
            "-e", f"HTTPS_PROXY={proxy_url}",
        ])
    
    if proxy_host and proxy_port:
        run_args.append("-e")
        run_args.append(f"GRADLE_OPTS=-Dhttp.proxyHost={proxy_host} -Dhttp.proxyPort={proxy_port} -Dhttps.proxyHost={proxy_host} -Dhttps.proxyPort={proxy_port}")
    env = DockerEnvironment(
        image=image, 
        cwd=cwd, 
        timeout=timeout, 
        run_args=run_args, 
        use_sudo=True
    )
    
    env.execute("git config --global user.email 'you@example.com'")
    env.execute("git config --global user.name 'Your Name'")
    
    if proxy_url:
        proxy_setup = f"""
export http_proxy={proxy_url}
export https_proxy={proxy_url}
export HTTP_PROXY={proxy_url}
export HTTPS_PROXY={proxy_url}

echo 'export http_proxy={proxy_url}' >> ~/.bashrc
echo 'export https_proxy={proxy_url}' >> ~/.bashrc
echo 'export HTTP_PROXY={proxy_url}' >> ~/.bashrc
echo 'export HTTPS_PROXY={proxy_url}' >> ~/.bashrc
"""
        env.execute(proxy_setup)
    
    if proxy_host and proxy_port:
        gradle_opts = f"-Dhttp.proxyHost={proxy_host} -Dhttp.proxyPort={proxy_port} -Dhttps.proxyHost={proxy_host} -Dhttps.proxyPort={proxy_port}"
        proxy_setup_gradle = f"""
export GRADLE_OPTS="{gradle_opts}"
echo 'export GRADLE_OPTS="{gradle_opts}"' >> ~/.bashrc
"""
        env.execute(proxy_setup_gradle)
        
        gradle_proxy_config = f"""
mkdir -p ~/.gradle
cat > ~/.gradle/gradle.properties << 'EOF'
systemProp.http.proxyHost={proxy_host}
systemProp.http.proxyPort={proxy_port}
systemProp.https.proxyHost={proxy_host}
systemProp.https.proxyPort={proxy_port}
EOF
"""
        env.execute(gradle_proxy_config)
        
        maven_proxy_config = f"""
mkdir -p ~/.m2
cat > ~/.m2/settings.xml << 'EOF'
<settings>
  <proxies>
    <proxy>
      <id>http-proxy</id>
      <active>true</active>
      <protocol>http</protocol>
      <host>{proxy_host}</host>
      <port>{proxy_port}</port>
    </proxy>
    <proxy>
      <id>https-proxy</id>
      <active>true</active>
      <protocol>https</protocol>
      <host>{proxy_host}</host>
      <port>{proxy_port}</port>
    </proxy>
  </proxies>
</settings>
EOF
"""
        env.execute(maven_proxy_config)
    
    return env


def _apply_candidate_in_validator(env: DockerEnvironment, code_patch: str, test_patch: str, candidate_diff: str, test_spec=None, instance_id: str = "") -> dict:
    _write_and_apply_patch(env, code_patch, "code")
    _write_and_apply_patch(env, test_patch, "test")

    if instance_id.startswith("babel__babel"):
        needs_rebuild = False
        for patch_content in [code_patch, test_patch]:
            if patch_content and ('.ts' in patch_content or 'packages/' in patch_content):
                if re.search(r'diff --git a/packages/.*\.ts', patch_content):
                    needs_rebuild = True
                    break
        
        if needs_rebuild:
            build_result = env.execute("cd /testbed && make build 2>&1 | tail -30")
            if build_result.get("returncode") != 0:
                pass

    commit_result = env.execute("git add -A && git commit --allow-empty -m 'chore: apply baseline patches for testing'")
    if commit_result.get("returncode", 1) != 0:
        return {"ok": False, "reason": "baseline_commit_failed", "error": commit_result.get("stderr", "")}

    if test_spec:
        _run_build_commands_from_test_spec(env, test_spec, "after applying patches", instance_id)

    if candidate_diff.strip():
        apply_result = _write_and_apply_patch(env, candidate_diff, "candidate")
        if "failed" in apply_result.lower():
            return {"ok": False, "reason": "candidate_apply_failed", "error": apply_result}
        
        if test_spec:
            _run_build_commands_from_test_spec(env, test_spec, "after applying candidate", instance_id)
        
        if instance_id.startswith("babel__babel"):
            if candidate_diff and re.search(r'diff --git a/packages/.*\.ts', candidate_diff):
                build_result = env.execute("cd /testbed && make build 2>&1 | tail -30")
                if build_result.get("returncode") != 0:
                    pass
    else:
        return {"ok": False, "reason": "empty_candidate"}
    
    return {"ok": True}


def process_instance(
    instance: dict,
    output_dir: Path,
    model_name: str | None,
    config_path: str | Path,
    progress_manager: RunBatchProgressManager,
    base_url: str | None = None,
    api_key: str | None = None,
    self_deployed_ip: str | None = None,
    prompt_builder: str | None = None,
    retry_limit: int = 3,
) -> None:
    instance_id = instance["instance_id"]
    instance_dir = output_dir / instance_id
    remove_from_preds_file(output_dir / "preds.json", instance_id)
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
        except Exception:
            pass

    progress_manager.on_instance_start(instance_id)
    progress_manager.update_instance_status(instance_id, "Pulling/starting docker")

    entry = _load_test_patches().get(instance_id, {})
    code_patch = entry.get("patch", "")
    test_patch = entry.get("test_patch", "")
    test_files: list[str] = entry.get("test_files", [])
    allowed_files: list[str] = entry.get("files", [])
    repo_desc: str = entry.get("repo_description", instance.get("problem_statement", ""))
    f2p_tests: list[str] = entry.get("F2P", [])
    p2p_tests: list[str] = entry.get("P2P", [])

    try:
        test_spec = make_test_spec(instance)
    except Exception as e:
        progress_manager.on_instance_end(instance_id, "Error creating test spec")
        return

    hack_results: list[dict] = []

    strategy_groups = [
        ("B", "Boundaries", ["B1", "B2", "B3"]),
        ("C", "Types", ["C1", "C2", "C3"]),
    ]

    try:
        import shutil

        completed_groups = _get_completed_strategy_groups(output_dir / "preds.json", instance_id)

        total_rounds = len(strategy_groups)
        processed_rounds = 0
        for round_idx, (group_code, group_name, strategies) in enumerate(strategy_groups, 1):
            if group_code in completed_groups:
                continue
            
            processed_rounds += 1
            remaining_groups = [g[0] for g in strategy_groups if g[0] not in completed_groups]
            progress_manager.update_instance_status(instance_id, f"Processing {group_code}/{len(remaining_groups)} remaining: {group_name}")
        
            round_ipc_host = instance_dir / f"ipc_round{round_idx}_{uuid.uuid4().hex[:8]}"
            container_ipc = "/ipc"

            env_inj = _start_env(image_name, "/testbed", round_ipc_host, container_ipc, timeout=600, instance_id=instance_id)
            env_val = _start_env(image_name, "/testbed", round_ipc_host, container_ipc, timeout=600, instance_id=instance_id)

            try:
                progress_manager.update_instance_status(instance_id, f"Round {round_idx}/5: Preparing Injector")
                code_result = _write_and_apply_patch(env_inj, code_patch, "code")
                test_result = _write_and_apply_patch(env_inj, test_patch, "test")

                if "failed" in code_result.lower() or "failed" in test_result.lower():
                    raise RuntimeError(f"Round {round_idx}: Failed to apply patches in Injector: {code_result}, {test_result}")

                commit_result = env_inj.execute("git add -A && git commit --allow-empty -m 'chore: apply baseline patches for testing'")
                if commit_result.get("returncode", 1) != 0:
                    raise RuntimeError(f"Round {round_idx}: Failed to create baseline commit in Injector: {commit_result.get('stderr', '')}")

                progress_manager.update_instance_status(instance_id, f"Round {round_idx}/5: Setting up Validator")
                _setup_validator_env(env_val, test_spec, instance_id, f2p_tests=f2p_tests)

                round_dir = instance_dir / f"round_{round_idx}_{group_code}"
                round_dir.mkdir(parents=True, exist_ok=True)

                agent = ProgressTrackingInjectorAgent(
                    get_model(model_name, config=model_config),
                    env_inj,
                    progress_manager=progress_manager,
                    instance_id=instance_id,
                    **config.get("agent", {}),
                )

                test_cmd = ""
                f2p_test_cmd = ""
                try:
                    repo = instance["repo"]
                    version = instance["version"]
                    specs = MAP_REPO_VERSION_TO_SPECS[repo][version]
                    raw_test_cmd = specs.get("test_cmd", "Standard Test Suite")
                    
                    if isinstance(raw_test_cmd, list):
                        if f2p_tests:
                            for cmd in raw_test_cmd:
                                for f2p_test in f2p_tests:
                                    if f2p_test in cmd:
                                        f2p_test_cmd = cmd
                                        break
                                if f2p_test_cmd:
                                    break
                            if not f2p_test_cmd:
                                f2p_test_cmd = raw_test_cmd[0] if raw_test_cmd else "Standard Test Suite"
                        else:
                            f2p_test_cmd = raw_test_cmd[0] if raw_test_cmd else "Standard Test Suite"
                        
                        test_cmd = f"F2P Test: {f2p_test_cmd}"
                    else:
                        test_cmd = raw_test_cmd
                        f2p_test_cmd = raw_test_cmd
                except Exception:
                    test_cmd = "Standard Test Suite"
                    f2p_test_cmd = "Standard Test Suite"

                template_vars = {
                    "repo_description": repo_desc,
                    "test_files": test_files,
                    "allowed_files": allowed_files,
                    "strategy_group": group_code,
                    "strategy_group_name": group_name,
                    "allowed_strategies": strategies,
                    "test_cmd": test_cmd,
                }

                exit_state = ""
                diff_text = ""
                explan = ""

                try:
                    exit_state, payload = agent.run_once(repo_desc, template_vars=template_vars)
                except Exception as e:
                    exit_state, payload = type(e).__name__, str(e)

                def _parse_payload(s: str) -> tuple[str, str]:
                    try:
                        cleaned_s = s.replace('\r', '\\r').replace('\t', '\\t')
                        if cleaned_s.startswith('{') and cleaned_s.endswith('}'):
                            obj = json.loads(cleaned_s)
                            diff = obj.get("diff", "")
                            explanation = obj.get("explanation", "")
                            return diff, explanation
                        else:
                            return s, ""
                    except json.JSONDecodeError as e:
                        try:
                            diff_start = s.find('"diff":')
                            if diff_start == -1:
                                return s, ""
                            
                            value_start = s.find('"', diff_start + 7)
                            if value_start == -1:
                                return s, ""
                            value_start += 1
                            
                            expl_marker = '"explanation":'
                            expl_start = s.find(expl_marker, value_start)
                            if expl_start == -1:
                                expl_start = s.rfind('}')
                                if expl_start == -1:
                                    return s, ""
                            
                            search_end = expl_start if expl_marker in s else len(s)
                            i = search_end - 1
                            
                            while i > value_start:
                                if s[i] == '"':
                                    num_backslashes = 0
                                    j = i - 1
                                    while j >= value_start and s[j] == '\\':
                                        num_backslashes += 1
                                        j -= 1
                                    if num_backslashes % 2 == 0:
                                        value_end = i
                                        break
                                i -= 1
                            else:
                                return s, ""
                            
                            diff_content = s[value_start:value_end]
                            diff_content = diff_content.replace('\\\\', '\x00')
                            diff_content = diff_content.replace('\\n', '\n')
                            diff_content = diff_content.replace('\\t', '\t')
                            diff_content = diff_content.replace('\\r', '\r')
                            diff_content = diff_content.replace('\\"', '"')
                            diff_content = diff_content.replace('\x00', '\\')
                            
                            return diff_content, ""
                            
                        except Exception:
                            match = re.search(r'"diff":\s*"([^"]+)"', s)
                            if match:
                                diff_content = match.group(1)
                                diff_content = diff_content.replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"').replace('\\\\', '\\')
                                return diff_content, ""
                            else:
                                return s, ""
                    except Exception:
                        return s, ""

                diff_text, explan = _parse_payload(payload)

                attempt = 0
                accepted = False
                veri_details: dict = {}
                while attempt <= retry_limit:
                    attempt += 1
                    progress_manager.update_instance_status(instance_id, f"Round {round_idx}/5: {group_name} - Attempt {attempt}")

                    _write_ipc(round_ipc_host, "proposal.json", {
                        "round": round_idx,
                        "strategy_group": group_code,
                        "attempt": attempt,
                        "explanation": explan,
                    })
                    
                    if not diff_text.strip():
                        veri_details = {"ok": False, "reason": "empty_diff"}
                        _write_ipc(round_ipc_host, "validation.json", {"round": round_idx, "attempt": attempt, "result": veri_details})
                        continue
                    
                    if not diff_text.startswith('diff --git'):
                        veri_details = {"ok": False, "reason": "invalid_diff_format"}
                        _write_ipc(round_ipc_host, "validation.json", {"round": round_idx, "attempt": attempt, "result": veri_details})
                        continue
                    
                    (round_ipc_host / "proposal.patch").write_text(diff_text)

                    applied = _apply_candidate_in_validator(env_val, code_patch, test_patch, diff_text, test_spec, instance_id)
                    if not applied.get("ok"):
                        veri_details = {"ok": False, "reason": applied.get("reason", "apply_failed")}
                    else:
                        actual_test_cmd = ""
                        try:
                            repo = instance.get("repo", "")
                            version = instance.get("version", "")
                            if repo and version:
                                specs = MAP_REPO_VERSION_TO_SPECS.get(repo, {}).get(version, {})
                                actual_test_cmd = specs.get("test_cmd", "")
                        except Exception:
                            pass
                        
                        veri_details = _verify_candidate(env_val, test_spec, instance, f2p_tests, p2p_tests, actual_test_cmd)

                    _write_ipc(round_ipc_host, "validation.json", {"round": round_idx, "attempt": attempt, "result": veri_details})

                    if veri_details.get("ok"):
                        accepted = True
                        break

                    if attempt < retry_limit:
                        feedback = f"Validator rejected. Reason: {veri_details}. Try a different subtle bug from {group_name} strategy group ({strategies}) without touching tests, only allowed files: {allowed_files}. Keep it plausible and minimal. Output JSON with fields diff and explanation."
                        try:
                            exit_state, payload = agent.continue_with_feedback(feedback)
                        except Exception as e:
                            exit_state, payload = type(e).__name__, str(e)
                        diff_text, explan = _parse_payload(payload)
                    else:
                        break

                save_traj(
                    agent,
                    round_dir / f"{instance_id}__round{round_idx}_{group_code}.traj.json",
                    exit_status=exit_state if accepted else f"Rejected_{exit_state}",
                    result=diff_text,
                    extra_info={
                        "round": round_idx,
                        "strategy_group": group_code,
                        "strategy_group_name": group_name,
                        "allowed_strategies": strategies,
                        "accepted": accepted,
                        "validation": veri_details,
                        "explanation": explan,
                        "attempts": attempt,
                    },
                    instance_id=f"{instance_id}:round{round_idx}_{group_code}",
                )

                hack_results.append({
                    "round": round_idx,
                    "strategy_group": group_code,
                    "strategy_group_name": group_name,
                    "exit_status": exit_state,
                    "accepted": accepted,
                    "diff": diff_text,
                    "explanation": explan,
                    "validation": veri_details,
                    "attempts": attempt,
                })

                try:
                    resolved_model_name = (model_name or "")
                except Exception:
                    resolved_model_name = model_name or ""
                
                accepted_hacks = [hack for hack in hack_results if hack.get("accepted", False)]
                completed_groups = list(set(hack.get("strategy_group", "") for hack in accepted_hacks if hack.get("strategy_group")))
                
                update_preds_file(
                    output_dir / "preds.json",
                    instance_id,
                    resolved_model_name,
                    json.dumps({
                        "hacks": accepted_hacks,
                        "completed_groups": sorted(completed_groups)
                    }, ensure_ascii=False),
                )

            finally:
                try:
                    env_inj.cleanup()
                    env_val.cleanup()
                except Exception:
                    pass

                if round_ipc_host and round_ipc_host.exists():
                    try:
                        shutil.rmtree(round_ipc_host)
                    except Exception:
                        pass

        save_traj(
            None,
            instance_dir / f"{instance_id}.traj.json",
            exit_status="Finished",
            result=f"5 rounds processed, {sum(1 for hack in hack_results if hack.get('accepted', False))} accepted",
            extra_info={"hacks": hack_results},
            instance_id=instance_id,
        )
    except Exception as e:
        traceback.print_exc()
        save_traj(None, instance_dir / f"{instance_id}.traj.json", exit_status="Error", result=str(e), instance_id=instance_id)
    finally:
        if hack_results:
            try:
                resolved_model_name = (model_name or "")
            except Exception:
                resolved_model_name = model_name or ""
            
            accepted_hacks = [hack for hack in hack_results if hack.get("accepted", False)]
            completed_groups = list(set(hack.get("strategy_group", "") for hack in accepted_hacks if hack.get("strategy_group")))
            
            update_preds_file(
                output_dir / "preds.json",
                instance_id,
                resolved_model_name,
                json.dumps({
                    "hacks": accepted_hacks,
                    "completed_groups": sorted(completed_groups)
                }, ensure_ascii=False),
            )

        progress_manager.on_instance_end(instance_id, "Finished")


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


@app.command(help="Run two-stage Inject-Validate hack mode (dual containers, per-target retries).")
def main(
    subset: str = typer.Option("lite", "--subset"),
    split: str = typer.Option("dev", "--split"),
    slice_spec: str = typer.Option("", "--slice"),
    filter_spec: str = typer.Option("", "--filter"),
    shuffle: bool = typer.Option(False, "--shuffle"),
    output: str = typer.Option("", "-o", "--output"),
    workers: int = typer.Option(1, "-w", "--workers"),
    model: str | None = typer.Option(None, "-m", "--model"),
    config: Path = typer.Option(builtin_config_dir / "extra" / "swehack_injector.yaml", "-c", "--config"),
    retry_limit: int = typer.Option(2, "--retry-limit"),
    base_url: str | None = typer.Option(None, "--base-url"),
    api_key: str | None = typer.Option(None, "--api-key"),
    self_deployed_ip: str | None = typer.Option(None, "--self-deployed-ip"),
    prompt_builder: str | None = typer.Option(None, "--prompt-builder"),
    start_index: int = typer.Option(0, "--start-index"),
    num_instances: int | None = typer.Option(None, "--num-instances"),
    suffix: str = typer.Option("", "--suffix"),
    skip_existing: bool = typer.Option(False, "--skip-existing", help="Skip instances that already exist in output preds.json file"),
) -> None:
    print(f"Loading all instance IDs from {TEST_PATCHES_PATH} ...")
    all_instance_ids = _load_all_instance_ids()
    if not all_instance_ids:
        print("No instances found in test_patches.jsonl.")
        raise typer.Exit(code=1)
    print(f"Found {len(all_instance_ids)} total instances in test_patches.jsonl")

    selected_instance_ids = all_instance_ids.copy()

    if start_index > 0 or num_instances is not None:
        end_index = start_index + num_instances if num_instances is not None else None
        selected_instance_ids = selected_instance_ids[start_index:end_index]
        print(f"Selected instances from index {start_index} to {end_index or 'end'}: {len(selected_instance_ids)} instances")

    dataset_path = DATASET_MAPPING.get(subset, subset)
    print(f"Loading dataset {dataset_path}, split {split}...")
    dataset_instances = list(load_dataset(dataset_path, split=split))
    dataset_dict = {instance["instance_id"]: instance for instance in dataset_instances}

    instances = []
    for instance_id in selected_instance_ids:
        if instance_id in dataset_dict:
            instances.append(dataset_dict[instance_id])
        else:
            print(f"Warning: Instance {instance_id} not found in dataset")

    print(f"Found {len(instances)} instances in dataset for selected IDs")

    instances = filter_instances(instances, filter_spec=filter_spec, slice_spec=slice_spec, shuffle=shuffle)

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
            except Exception:
                pass
        temp_model = get_model(model, config=model_config)
        model_name = temp_model.config.model_name.replace("/", "_").replace(":", "_")
        output = f"./results/{model_name}"
        print(f"Auto-generated output path: {output}")

    if suffix:
        p = Path(output)
        output = str(p.parent / f"{p.name}_{suffix}")

    if skip_existing:
        preds_file = Path(output) / "preds.json"
        
        before_skip = len(instances)
        instances_to_process = []
        for instance in instances:
            completed_groups = _get_completed_strategy_groups(preds_file, instance["instance_id"])
            if len(completed_groups) < 2:
                instances_to_process.append(instance)
            else:
                pass
        
        instances = instances_to_process
        after_skip = len(instances)

        if before_skip != after_skip:
            print(f"Skip existing (fine-grained): {before_skip} -> {after_skip} instances (skipped {before_skip - after_skip} fully completed instances)")

    output_path = Path(output)
    output_path.mkdir(parents=True, exist_ok=True)
    print(f"Running on {len(instances)} instances...")
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
                    process_instance,
                    instance,
                    output_path,
                    model,
                    config,
                    progress_manager,
                    base_url,
                    api_key,
                    self_deployed_ip,
                    prompt_builder,
                    retry_limit,
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
