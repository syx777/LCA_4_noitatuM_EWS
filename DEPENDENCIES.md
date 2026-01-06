# External Dependencies

## Required External Files and Modules

### 1. swe-bench
- **Path**: `${SWE_BENCH_PATH}` (configurable)
- **Modules Used**:
  - `swebench.harness.constants.MAP_REPO_VERSION_TO_SPECS`
  - `swebench.harness.test_spec.test_spec.make_test_spec`
  - `swebench.harness.log_parsers.MAP_REPO_TO_PARSER`
- **Description**: SWE-bench harness for test specification and parsing

### 2. mini-swe-agent
- **Path**: Installed as Python package
- **Modules Used**:
  - `minisweagent.agents.default.DefaultAgent`
  - `minisweagent.agents.default.NonTerminatingException`
  - `minisweagent.agents.default.TerminatingException`
  - `minisweagent.config.builtin_config_dir`
  - `minisweagent.config.get_config_path`
  - `minisweagent.environments.docker.DockerEnvironment`
  - `minisweagent.models.get_model`
  - `minisweagent.run.extra.utils.batch_progress.RunBatchProgressManager`
  - `minisweagent.run.utils.save.save_traj`
- **Description**: Agent framework for software engineering tasks

### 3. Test Patches File
- **Path**: `${TEST_PATCHES_PATH}` (configurable)
- **Format**: JSONL file with one JSON object per line
- **Required Fields**:
  - `instance_id` (or `id` or `name`)
  - `patch`: Code patch string
  - `test_patch`: Test patch string
  - `test_files`: List of test file paths
  - `files`: List of allowed files for mutation
  - `FAIL_TO_PASS`: List of F2P test identifiers
  - `PASS_TO_PASS`: List of P2P test identifiers
  - `repo_description` (or `problem_statement`): Repository description

### 4. Configuration File
- **Path**: Default: `builtin_config_dir / "extra" / "swehack_injector.yaml"`
- **Format**: YAML
- **Required Sections**:
  - `model`: Model configuration
  - `agent`: Agent configuration
  - System and instance templates (referenced in agent config)

### 5. Environment Variables
- `SWE_BENCH_PATH`: Path to swe-bench repository (optional, defaults to empty)
- `TEST_PATCHES_PATH`: Path to test patches JSONL file (optional, defaults to "test_patches.jsonl")
- `PROXY_URL`: Proxy URL for network operations (optional, e.g., "http://proxy.example.com:8080")
- `PROXY_HOST`: Proxy host address (optional, used for Gradle/Maven proxy config)
- `PROXY_PORT`: Proxy port number (optional, used for Gradle/Maven proxy config)

## Python Package Dependencies

- `typer`: CLI framework
- `yaml`: YAML parsing
- `datasets`: HuggingFace datasets library
- `rich`: Rich text and progress display
- `concurrent.futures`: Parallel execution
- Standard library: `json`, `re`, `shlex`, `sys`, `threading`, `time`, `traceback`, `uuid`, `pathlib`

