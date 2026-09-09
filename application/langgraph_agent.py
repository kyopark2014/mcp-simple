import hashlib
import logging
import re
import sys
import traceback
import chat
import utils
import skill
import mcp_config
import subprocess
import datetime

from typing import Literal, Optional
from langgraph.graph import START, END, StateGraph
from typing_extensions import Annotated, TypedDict
from langgraph.graph.message import add_messages
from langchain_core.prompts import MessagesPlaceholder, ChatPromptTemplate
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage, SystemMessage
from langchain_core.messages.base import BaseMessage, BaseMessageChunk
from langchain_core.messages.ai import AIMessage, AIMessageChunk
from langchain.mcp import MCPAdapter
from notification_queue import NotificationQueue
from pytz import timezone
from langgraph.prebuilt import ToolNode

import io, os, json
import subprocess as _subprocess, pathlib as _pathlib, shutil as _shutil
import tempfile as _tempfile, glob as _glob, datetime as _datetime
import math as _math, re as _re, requests as _requests
from urllib.parse import quote
from langchain_core.tools import tool

logging.basicConfig(
    level=logging.INFO,
    format='%(filename)s:%(lineno)d | %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger("langgraph_agent")

config = utils.load_config()
sharing_url = config.get("sharing_url") or getattr(utils, "sharing_url", None)
s3_prefix = "docs"
user_id = "langgraph"

WORKING_DIR = os.path.dirname(os.path.abspath(__file__))
ARTIFACTS_DIR = os.path.join(WORKING_DIR, "artifacts")

_ARTIFACT_EXT = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx"})

_mpl_runtime_ready = False


def _ensure_cli_scripts_on_path() -> None:
    """Prepend pip user script dir so CLIs resolve in subprocess."""
    import site
    import sysconfig

    extra: list[str] = []
    user_base = getattr(site, "USER_BASE", None)
    if user_base:
        user_bin = os.path.join(user_base, "bin")
        if os.path.isdir(user_bin):
            extra.append(user_bin)
    try:
        scripts = sysconfig.get_path("scripts")
        if scripts and os.path.isdir(scripts):
            extra.append(scripts)
    except Exception:
        pass
    path = os.environ.get("PATH", "")
    parts = [p for p in path.split(os.pathsep) if p]
    for d in reversed(extra):
        if d and d not in parts:
            parts.insert(0, d)
    os.environ["PATH"] = os.pathsep.join(parts)



def _ensure_user_site_on_sys_path() -> None:
    """Expose pip --user site-packages to this process (and PYTHONPATH for children).

    Runtime runs as appuser, so `pip install` lands under ~/.local. site.py only
    adds USER_SITE at interpreter startup if that directory already exists; packages
    installed later (bash/execute_code) stay invisible until we addsitedir here.
    """
    import site

    try:
        user_site = site.getusersitepackages()
    except Exception:
        return
    if not user_site or not os.path.isdir(user_site):
        return

    # addsitedir also honors .pth files; skip if already on path
    if user_site not in sys.path:
        site.addsitedir(user_site)

    py_path = os.environ.get("PYTHONPATH", "")
    parts = [p for p in py_path.split(os.pathsep) if p]
    if user_site not in parts:
        parts.insert(0, user_site)
        os.environ["PYTHONPATH"] = os.pathsep.join(parts)


class _UserSiteRefreshFinder:
    """Refresh USER_SITE on import so mid-exec `pip install` + `import` works."""

    def find_spec(self, fullname, path=None, target=None):  # noqa: ARG002
        import site

        try:
            user_site = site.getusersitepackages()
        except Exception:
            return None
        if user_site and os.path.isdir(user_site) and user_site not in sys.path:
            _ensure_user_site_on_sys_path()
        return None


def _install_user_site_import_hook() -> None:
    if any(isinstance(f, _UserSiteRefreshFinder) for f in sys.meta_path):
        return
    sys.meta_path.insert(0, _UserSiteRefreshFinder())


_install_user_site_import_hook()


def _artifact_files_mtime_snapshot() -> dict:
    """WORKING_DIR 기준 상대 경로 -> mtime. artifacts/ 이하만 스캔."""
    snap = {}
    if not os.path.isdir(ARTIFACTS_DIR):
        return snap
    for dirpath, _, filenames in os.walk(ARTIFACTS_DIR):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            try:
                rel = os.path.relpath(full, WORKING_DIR)
                snap[rel] = os.path.getmtime(full)
            except OSError:
                pass
    return snap


def _touched_artifact_paths(before: dict, after: dict) -> list:
    """실행 전후 스냅샷 차이로 새로 생기거나 수정된 파일만."""
    touched = []
    for rel, mt in after.items():
        if rel not in before or before[rel] != mt:
            touched.append(rel)
    return sorted(touched)


def _paths_for_ui(relative_paths: list) -> list:
    """sharing_url이 있으면 공개 URL, 없으면 Streamlit st.image용 절대 경로."""
    out = []
    base = sharing_url.rstrip("/") if sharing_url else ""
    for rel in relative_paths:
        if base:
            out.append(f"{base}/{quote(rel)}")
        else:
            out.append(os.path.abspath(os.path.join(WORKING_DIR, rel)))
    return out

def _ensure_matplotlib_runtime():
    """Use non-interactive Agg backend, prefer CJK-capable fonts, silence headless/show noise."""
    global _mpl_runtime_ready
    if _mpl_runtime_ready:
        return
    try:
        import matplotlib

        matplotlib.use("Agg")

        import warnings

        warnings.filterwarnings(
            "ignore",
            message=r"Glyph .* missing from font",
            category=UserWarning,
        )
        warnings.filterwarnings(
            "ignore",
            message=r"FigureCanvasAgg is non-interactive.*",
            category=UserWarning,
        )

        import matplotlib.font_manager as fm
        import matplotlib as mpl

        mpl.rcParams["axes.unicode_minus"] = False
        cjk_candidates = (
            "AppleGothic",
            "Apple SD Gothic Neo",
            "Malgun Gothic",
            "NanumGothic",
            "NanumBarunGothic",
            "Noto Sans CJK KR",
            "Noto Sans KR",
        )
        mpl.rcParams["font.family"] = "sans-serif"
        mpl.rcParams["font.sans-serif"] = list(cjk_candidates) + ["DejaVu Sans", "sans-serif"]

        _mpl_runtime_ready = True
    except Exception as e:
        logger.info(f"matplotlib runtime setup skipped: {e}")
        _mpl_runtime_ready = True

_exec_globals = {
    "__builtins__": __builtins__,
    "subprocess": _subprocess,
    "json": json,
    "os": os,
    "sys": sys,
    "io": io,
    "pathlib": _pathlib,
    "shutil": _shutil,
    "tempfile": _tempfile,
    "glob": _glob,
    "datetime": _datetime,
    "math": _math,
    "re": _re,
    "requests": _requests,
    "WORKING_DIR": WORKING_DIR,
    "ARTIFACTS_DIR": ARTIFACTS_DIR,
}

_KOREAN_WEEKDAYS = ("월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일")

@tool
def get_current_time(format: str=f"%Y-%m-%d %H:%M:%S")->str:
    """Returns the current date and time in Asia/Seoul, including the Korean weekday.

    Example: "2026-08-08 15:51:06 (토요일)"
    """
    format = format.replace('\'','')
    now = datetime.datetime.now(timezone('Asia/Seoul'))
    timestr = f"{now.strftime(format)} ({_KOREAN_WEEKDAYS[now.weekday()]})"
    logger.info(f"timestr: {timestr}")
    
    return timestr

@tool
def execute_code(code: str) -> str:
    """Execute Python code and return stdout/stderr output.

    Use this tool to run Python code for tasks such as processing data,
    processing data, or performing computations. The execution environment
    has access to common libraries: pandas, numpy, matplotlib, seaborn, etc.
    json, csv, os, requests, etc.

    Variables and imports from previous calls persist across invocations.
    Generated files should be saved to the 'artifacts/' directory.

    Path variables (pre-defined, do NOT redefine):
    - WORKING_DIR: absolute path to application directory
    - ARTIFACTS_DIR: absolute path to artifacts directory (WORKING_DIR/artifacts)

    Args:
        code: Python code to execute.

    Returns:
        Captured stdout output, or error traceback if execution failed.
        If there is a result file, return the path of the file.            
    """
    logger.info(f"###### execute_code ######")
    os.makedirs(ARTIFACTS_DIR, exist_ok=True)
    before_files = _artifact_files_mtime_snapshot()

    old_cwd = os.getcwd()
    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()

    try:
        os.chdir(WORKING_DIR)
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = stdout_capture, stderr_capture

        _ensure_matplotlib_runtime()
        exec(code, _exec_globals)

        sys.stdout, sys.stderr = old_stdout, old_stderr
        os.chdir(old_cwd)

        output = stdout_capture.getvalue()
        errors = stderr_capture.getvalue()

        result = ""
        if output:
            result += output
        if errors:
            result += f"\n[stderr]\n{errors}"
        if not result.strip():
            result = "Code executed successfully (no output)."

        after_files = _artifact_files_mtime_snapshot()
        touched = _touched_artifact_paths(before_files, after_files)
        artifact_rels = [
            r
            for r in touched
            if os.path.splitext(r)[1].lower() in _ARTIFACT_EXT
        ]
        other_rels = [r for r in touched if r not in artifact_rels]
        if other_rels:
            lines = "\n".join(
                os.path.abspath(os.path.join(WORKING_DIR, r)) for r in other_rels
            )
            result += f"\n[artifacts]\n{lines}"

        if artifact_rels:
            payload = {"output": result.strip()}
            payload["path"] = _paths_for_ui(artifact_rels)
            return json.dumps(payload, ensure_ascii=False)

        return result

    except Exception as e:
        sys.stdout, sys.stderr = old_stdout, old_stderr
        os.chdir(old_cwd)
        tb = traceback.format_exc()
        logger.error(f"Code execution error: {tb}")
        return f"Error executing code:\n{tb}"

@tool
def write_file(filepath: str, content: str = "") -> str:
    """Write text content to a file.

    CRITICAL: content must always be passed. Calling without content will fail.
    Never call without content. Both filepath and content are required in a single call.

    Args:
        filepath: Absolute path or path relative to WORKING_DIR.
        content: The text content to write. REQUIRED - must not be omitted. Must include full file content.

    Returns:
        A success or failure message.
    """
    if not content:
        return (
            "Error: content parameter is required. "
            "Pass the full content to save in the form write_file(filepath='path', content='content_to_save')."
        )
    logger.info(f"###### write_file: {filepath} ######")
    try:
        full_path = filepath if os.path.isabs(filepath) else os.path.join(WORKING_DIR, filepath)
        parent = os.path.dirname(full_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)

        result_msg = f"File saved: {filepath}"
        return result_msg
    except Exception as e:
        return f"Failed to save file: {str(e)}"


@tool
def read_file(filepath: str) -> str:
    """Read the contents of a local file.

    Args:
        filepath: Absolute path or path relative to WORKING_DIR.

    Returns:
        The file contents as text, or an error message.
    """
    logger.info(f"###### read_file: {filepath} ######")
    try:
        full_path = filepath if os.path.isabs(filepath) else os.path.join(WORKING_DIR, filepath)
        with open(full_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Failed to read file: {str(e)}"


@tool
def upload_file_to_s3(filepath: str) -> str:
    """Upload a local file to S3 and return the download URL.

    Args:
        filepath: Path relative to the working directory (e.g. 'artifacts/report.pdf').

    Returns:
        The download URL, or an error message.
    """
    logger.info(f"###### upload_file_to_s3: {filepath} ######")
    try:
        import boto3
        from urllib import parse as url_parse

        s3_bucket = config.get("s3_bucket") or getattr(utils, "s3_bucket", None)
        if not s3_bucket:
            return "S3 bucket is not configured."

        full_path = filepath if os.path.isabs(filepath) else os.path.join(WORKING_DIR, filepath)
        if not os.path.exists(full_path):
            # also try relative to WORKING_DIR when agent passes artifacts/...
            alt = os.path.join(WORKING_DIR, filepath.lstrip("/"))
            if os.path.exists(alt):
                full_path = alt
            else:
                return f"File not found: {filepath}"

        # Normalize S3 key to path relative to WORKING_DIR when possible
        try:
            s3_key = os.path.relpath(full_path, WORKING_DIR)
        except ValueError:
            s3_key = os.path.basename(full_path)
        if s3_key.startswith(".."):
            s3_key = os.path.basename(full_path)

        content_type = utils.get_contents_type(s3_key)
        s3 = boto3.client("s3", region_name=config.get("region", "us-west-2"))

        with open(full_path, "rb") as f:
            s3.put_object(Bucket=s3_bucket, Key=s3_key, Body=f.read(), ContentType=content_type)

        effective_sharing = sharing_url or getattr(utils, "sharing_url", None) or config.get("sharing_url")
        if effective_sharing:
            url = f"{effective_sharing.rstrip('/')}/{url_parse.quote(s3_key)}"
            return f"Upload complete: {url}"
        return f"Upload complete: {chat.s3_uri_to_console_url(f's3://{s3_bucket}/{s3_key}', config.get('region', 'us-west-2'))}"

    except Exception as e:
        return f"Upload failed: {str(e)}"


@tool
def bash(command: str) -> str:
    """Execute a bash command and return the result.

    Use this for shell commands needed by skills (e.g. CLI tools, package installs).
    Working directory is the application folder.

    Args:
        command: Shell command to run.

    Returns:
        Captured stdout/stderr, or an error message.
    """
    logger.info(f"###### bash: {command} ######")
    _ensure_cli_scripts_on_path()
    _ensure_user_site_on_sys_path()
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            cwd=WORKING_DIR, timeout=300,
            env=os.environ,
        )
        # pip install may have just created ~/.local/.../site-packages
        _ensure_user_site_on_sys_path()
    except subprocess.TimeoutExpired:
        return "Error: command timed out after 300 seconds."
    parts = []
    if result.stdout:
        parts.append(f"STDOUT:\n{result.stdout}")
    if result.stderr:
        parts.append(f"STDERR:\n{result.stderr}")
    if result.returncode != 0:
        parts.append(f"Return code: {result.returncode}")
    return "\n".join(parts) if parts else "(no output)"


def get_builtin_tools() -> list:
    """Return the list of built-in tools for the skill-aware agent."""
    # Prefer live values from utils (auto-filled s3_bucket / sharing_url)
    effective_sharing = sharing_url or getattr(utils, "sharing_url", None) or config.get("sharing_url")
    effective_bucket = config.get("s3_bucket") or getattr(utils, "s3_bucket", None)

    tools = [execute_code, write_file, read_file, bash, get_current_time]
    if effective_sharing or effective_bucket:
        tools.append(upload_file_to_s3)
    return tools

def message_chunk_to_message(chunk: BaseMessage) -> BaseMessage:
    """Convert a message chunk to a `Message`.

    Args:
        chunk: Message chunk to convert.

    Returns:
        Message.
    """
    if not isinstance(chunk, BaseMessageChunk):
        return chunk
    # chunk classes always have the equivalent non-chunk class as their first parent
    ignore_keys = ["type"]
    if isinstance(chunk, AIMessageChunk):
        ignore_keys.extend(["tool_call_chunks", "chunk_position"])
    return chunk.__class__.__mro__[1](
        **{k: v for k, v in chunk.__dict__.items() if k not in ignore_keys}
    )

class State(TypedDict):
    messages: Annotated[list, add_messages]
    image_url: list

BASE_SYSTEM_PROMPT = (
    "당신의 이름은 서연이고, 질문에 친근한 방식으로 대답하도록 설계된 대화형 AI입니다.\n"
    "상황에 맞는 구체적인 세부 정보를 충분히 제공합니다.\n"
    "모르는 질문을 받으면 솔직히 모른다고 말합니다.\n"
    "한국어로 답변하세요."
)

# Bedrock Anthropic/Nova prompt caching (ephemeral, 1h TTL).
PROMPT_CACHE_CONTROL = {"type": "ephemeral", "ttl": "1h"}
GPT_PROMPT_CACHE_OPTIONS = {"mode": "explicit", "ttl": "30m"}


def _supports_bedrock_prompt_caching(model_type: str | None) -> bool:
    return model_type in ("claude", "nova")


def _supports_gpt_explicit_caching(model_type: str | None, model_id: str | None) -> bool:
    if model_type != "openai":
        return False
    mid = (model_id or "").lower()
    if mid.startswith("us.openai.") or mid.startswith("global.openai."):
        return False
    match = re.search(r"openai\.gpt-(\d+)\.(\d+)", mid)
    if not match:
        return False
    major, minor = int(match.group(1)), int(match.group(2))
    return (major, minor) >= (5, 6)


def _gpt_prompt_cache_key(config: dict, tools: list | None) -> str:
    cfg = config.get("configurable") or {}
    thread_id = cfg.get("thread_id") or "default"
    tool_names = sorted(getattr(t, "name", str(t)) for t in (tools or []))
    tools_digest = hashlib.sha256(",".join(tool_names).encode()).hexdigest()[:12]
    project = config.get("projectName") or utils.load_config().get("projectName") or "default"
    return f"{project}:{thread_id}:{tools_digest}"


def _system_message_with_bedrock_cache(system: str) -> SystemMessage:
    """Build a SystemMessage for Bedrock prompt caching.

    Do **not** embed ``cache_control`` on the system content block.

    ``ChatBedrock`` (InvokeModel) strips ``ttl`` from system
    ``cache_control`` down to ``{"type": "ephemeral"}``, which Bedrock
    treats as ``5m``. Combined with ``model.bind(cache_control=… ttl=1h)``
    on the last message, that violates Anthropic's rule that longer TTLs
    must precede shorter ones (tools → system → messages) and raises
    ``ValidationException``.

    ``ChatBedrockConverse`` ignores Anthropic-format ``cache_control`` on
    system text and applies matching ``cachePoint`` blocks (tools /
    system / last message) from ``bind(cache_control=PROMPT_CACHE_CONTROL)``
    alone — so a plain SystemMessage is correct for both wrappers.
    """
    return SystemMessage(content=system)


def _system_message_with_gpt_cache(system: str) -> SystemMessage:
    return SystemMessage(
        content=[
            {
                "type": "text",
                "text": system,
                "prompt_cache_breakpoint": {"mode": "explicit"},
            }
        ]
    )


def _log_prompt_cache_usage(response: AIMessage) -> None:
    usage = getattr(response, "usage_metadata", None) or {}
    details = usage.get("input_token_details") if isinstance(usage, dict) else None
    if not isinstance(details, dict):
        return
    cache_read = details.get("cache_read") or details.get("cached_tokens") or 0
    cache_creation = details.get("cache_creation") or details.get("cache_write_tokens") or 0
    if cache_read or cache_creation:
        logger.info(
            "prompt cache usage: cache_read=%s cache_creation=%s",
            cache_read,
            cache_creation,
        )


async def call_model(state: State, config):
    logger.info(f"###### call_model ######")

    last_message = state['messages'][-1]
    logger.info(f"last message: {last_message}")

    image_url = state['image_url'] if 'image_url' in state else []

    tools = get_builtin_tools()

    cfg = config.get("configurable") or {}
    bound_tools = cfg.get("tools")
    if not bound_tools and isinstance(config, dict):
        bound_tools = config.get("tools") or []
    if bound_tools:
        tool_names = {tool.name for tool in tools}
        for bt in bound_tools:
            if bt.name not in tool_names:
                tools.append(bt)
            else:
                logger.info(f"tool {bt.name} already in tools")

    system_prompt = cfg.get("system_prompt")
    if not system_prompt and isinstance(config, dict):
        system_prompt = config.get("system_prompt")

    system = system_prompt if system_prompt else BASE_SYSTEM_PROMPT

    active_model_id = chat.model_id
    active_model_type = chat.model_type
    chatModel = chat.get_chat()

    model = chatModel.bind_tools(tools)
    use_bedrock_cache = _supports_bedrock_prompt_caching(active_model_type)
    use_gpt_cache = _supports_gpt_explicit_caching(active_model_type, active_model_id)
    if use_bedrock_cache:
        model = model.bind(cache_control=PROMPT_CACHE_CONTROL)
    elif use_gpt_cache:
        model = model.bind(
            prompt_cache_key=_gpt_prompt_cache_key(config, tools),
            prompt_cache_options=GPT_PROMPT_CACHE_OPTIONS,
        )

    try:
        messages = []
        for msg in state["messages"]:
            if isinstance(msg, ToolMessage):
                content = msg.content
                if isinstance(content, list):
                    text_parts = []
                    for item in content:
                        if isinstance(item, dict):
                            item_clean = {k: v for k, v in item.items() if k != 'id'}
                            if 'text' in item_clean:
                                text_parts.append(item_clean['text'])
                            elif 'content' in item_clean:
                                text_parts.append(str(item_clean['content']))
                        elif isinstance(item, str):
                            text_parts.append(item)
                    content = '\n'.join(text_parts) if text_parts else str(content)
                elif not isinstance(content, str):
                    content = str(content)

                tool_msg = ToolMessage(
                    content=content,
                    tool_call_id=msg.tool_call_id
                )
                messages.append(tool_msg)
            else:
                messages.append(msg)

        if use_bedrock_cache:
            system_msg = _system_message_with_bedrock_cache(system)
        elif use_gpt_cache:
            system_msg = _system_message_with_gpt_cache(system)
        else:
            system_msg = SystemMessage(content=system)
        model_messages = [system_msg, *messages]

        accumulated: AIMessageChunk | None = None
        async for chunk in model.astream(model_messages):
            if accumulated is None:
                accumulated = chunk
            else:
                accumulated = accumulated + chunk

        if accumulated is None:
            response = AIMessage(content="답변을 찾지 못하였습니다.")
        else:
            merged = message_chunk_to_message(accumulated)
            response = merged if isinstance(merged, AIMessage) else AIMessage(
                content=getattr(merged, "content", str(merged))
            )
        logger.info(f"response of call_model: {response}")
        _log_prompt_cache_usage(response)

    except Exception:
        response = AIMessage(content="답변을 찾지 못하였습니다.")

        err_msg = traceback.format_exc()
        logger.info(f"error message: {err_msg}")

    return {"messages": [response], "image_url": image_url}

async def should_continue(state: State, config) -> Literal["continue", "end"]:
    logger.info(f"###### should_continue ######")

    messages = state["messages"]    
    last_message = messages[-1]
    
    if isinstance(last_message, AIMessage) and last_message.tool_calls:
        tool_name = last_message.tool_calls[-1]['name']
        logger.info(f"--- CONTINUE: {tool_name} ---")

        tool_args = last_message.tool_calls[-1]['args']

        if last_message.content:
            logger.info(f"last_message: {last_message.content}")

        logger.info(f"tool_name: {tool_name}, tool_args: {tool_args}")

        return "continue"
    else:
        logger.info(f"--- END ---")
        return "end"

def buildChatAgent(tools):
    tool_node = ToolNode(tools, handle_tool_errors=True)

    workflow = StateGraph(State)

    workflow.add_node("agent", call_model)
    workflow.add_node("action", tool_node)
    workflow.add_edge(START, "agent")
    workflow.add_conditional_edges(
        "agent",
        should_continue,
        {
            "continue": "action",
            "end": END,
        },
    )
    workflow.add_edge("action", "agent")

    return workflow.compile() 

def buildChatAgentWithHistory(tools):
    tool_node = ToolNode(tools, handle_tool_errors=True)

    workflow = StateGraph(State)

    workflow.add_node("agent", call_model)
    workflow.add_node("action", tool_node)
    workflow.add_edge(START, "agent")
    workflow.add_conditional_edges(
        "agent",
        should_continue,
        {
            "continue": "action",
            "end": END,
        },
    )
    workflow.add_edge("action", "agent")

    return workflow.compile(
        checkpointer=chat.checkpointer,
        store=chat.memorystore
    )

def load_multiple_mcp_server_parameters(mcp_json: dict):
    """Build per-server configs compatible with langchain.mcp.MCPAdapter / MCPConfig."""
    mcpServers = mcp_json.get("mcpServers")
  
    server_info = {}
    if mcpServers is not None:
        for server_name, cfg in mcpServers.items():
            if cfg.get("type") in ("streamable_http", "http", "streamable-http"):
                server_info[server_name] = {                    
                    "transport": "http",
                    "url": cfg.get("url"),
                    "headers": cfg.get("headers", {})
                }
            else:
                command = cfg.get("command", "")
                args = cfg.get("args", [])
                env = cfg.get("env", {})
                
                server_info[server_name] = {
                    "transport": "stdio",
                    "command": command,
                    "args": args,
                    "env": env                    
                }
    return server_info


async def create_agent(mcp_servers: list, skill_list: list, history_mode: str = "Disable"):
    """Build LangGraph agent with builtin + MCP + optional Skill tools."""
    tools = get_builtin_tools()
    logger.info(f"builtin_tools count: {len(tools)}")

    mcp_json = mcp_config.load_selected_config(mcp_servers)
    server_params = load_multiple_mcp_server_parameters(mcp_json)

    try:
        mcp_tools = []
        for server_name, params in server_params.items():
            try:
                async with MCPAdapter({"mcpServers": {server_name: params}}) as adapter:
                    logger.info("MCP client is initialized successfully")
                    _tools = await adapter.list_tools()
                mcp_tools.extend(_tools)
            except Exception as _mcp_err:
                logger.error(f"Failed to load MCP server '{server_name}': {_mcp_err}")

        for t in mcp_tools:
            logger.info(f"mcp_tool: {t.name}")
            if t.name not in {x.name for x in tools}:
                tools.append(t)
            else:
                logger.info(f"mcp_tool of {t.name} already in tools")
    except Exception as e:
        logger.error(f"Error creating MCP client or getting tools: {e}")
        logger.info(f"Falling back to builtin tools only (count: {len(tools)})")

    if chat.skill_mode == "Enable":
        tools.extend(skill.get_skill_tools())
        skill_info = skill.get_skill_info(skill_list)
        logger.info(f"skill_info: {skill_info}")
        system_prompt = skill.build_skill_prompt(skill_info)
    else:
        system_prompt = BASE_SYSTEM_PROMPT

    tool_list = [t.name for t in tools] if tools else []
    logger.info(f"tool_list: {tool_list}")

    if not tools:
        logger.warning("No tools available")
        return None, None

    if history_mode == "Enable":
        app = buildChatAgentWithHistory(tools)
    else:
        app = buildChatAgent(tools)

    agent_config = {
        "recursion_limit": 100,
        "configurable": {
            "thread_id": chat.user_id,
            "tools": tools,
            "system_prompt": system_prompt,
        },
        "tools": tools,
        "system_prompt": system_prompt,
    }
    return app, agent_config


_app = None
_agent_config = None
_active_mcp_servers = []
_active_skills = []
_active_skill_mode = None
_current_id = None


async def run_langgraph_agent(
    query: str,
    mcp_servers: list,
    skill_list: Optional[list] = None,
    history_mode: str = "Disable",
    notification_queue: Optional[NotificationQueue] = None,
) -> tuple:
    """Run the MCP+Skills LangGraph agent and stream results to the UI queue."""
    global _app, _agent_config, _active_mcp_servers, _active_skills, _active_skill_mode, _current_id

    skill_list = skill_list or []
    queue = notification_queue if notification_queue else None
    if queue:
        queue.reset()

    image_url = []
    references = []

    if (
        _app is None
        or _active_mcp_servers != mcp_servers
        or _active_skills != skill_list
        or _active_skill_mode != chat.skill_mode
        or _current_id != chat.user_id
    ):
        _active_mcp_servers = mcp_servers
        _active_skills = skill_list
        _active_skill_mode = chat.skill_mode
        _current_id = chat.user_id
        _app, _agent_config = await create_agent(mcp_servers, skill_list, history_mode)

    if _app is None:
        logger.error("Failed to create agent - app is None")
        return "에이전트를 생성할 수 없습니다. MCP 서버 설정 또는 도구 구성을 확인해주세요.", []

    inputs = {"messages": [HumanMessage(content=query)]}

    result = ""
    tool_used = False
    tool_name = toolUseId = ""
    chat.tool_input_list.clear()
    stop_reason: str | None = None

    def remember_stop_reason(message) -> None:
        nonlocal stop_reason
        meta = getattr(message, "response_metadata", None) or {}
        if not isinstance(meta, dict):
            return
        reason = meta.get("stopReason") or meta.get("stop_reason")
        if reason:
            stop_reason = reason
            logger.info(f"[stop_reason] {stop_reason}")

    async for stream in _app.astream(inputs, _agent_config, stream_mode="messages"):
        if isinstance(stream[0], AIMessageChunk):
            message = stream[0]
            remember_stop_reason(message)
            if isinstance(message.content, list):
                for content_item in message.content:
                    if not isinstance(content_item, dict):
                        continue
                    if content_item.get("type") == "text":
                        text_content = content_item.get("text", "")
                        if tool_used:
                            result = text_content
                            tool_used = False
                        else:
                            result += text_content
                        if chat.debug_mode == "Enable" and queue:
                            queue.stream(result)

                    elif content_item.get("type") == "tool_use":
                        if "id" in content_item and "name" in content_item:
                            toolUseId = content_item.get("id", "")
                            tool_name = content_item.get("name", "")
                            logger.info(f"tool_name: {tool_name}, toolUseId: {toolUseId}")
                            if queue:
                                queue.register_tool(toolUseId, tool_name)

                        if "partial_json" in content_item:
                            partial_json = content_item.get("partial_json", "")
                            if toolUseId not in chat.tool_input_list:
                                chat.tool_input_list[toolUseId] = ""
                            chat.tool_input_list[toolUseId] += partial_json
                            if queue:
                                queue.tool_update(
                                    toolUseId,
                                    f"Tool: {tool_name}, Input: {chat.tool_input_list[toolUseId]}",
                                )

        elif isinstance(stream[0], ToolMessage):
            message = stream[0]
            logger.info(f"ToolMessage: {message.name}, {message.content}")
            tool_name = message.name
            toolResult = message.content
            toolUseId = message.tool_call_id
            if chat.debug_mode == "Enable":
                chat.add_notification(notification_queue, f"Tool Result: {toolResult}")
            tool_used = True

            content, urls, refs = chat.get_tool_info(tool_name, toolResult)
            if refs:
                references.extend(refs)
                logger.info(f"refs: {refs}")
            if urls:
                image_url.extend(urls)
                logger.info(f"urls: {urls}")
            if content:
                logger.info(f"content: {content}")

    # Final stop_reason wins even when earlier turns left preamble text
    # (e.g. "확인해보겠습니다" + tools, then empty refusal).
    if stop_reason == "content_filtered":
        result = (
            "요청이 모델 안전 정책에 의해 차단되었습니다. "
            "다른 모델로 시도하거나 질문을 바꿔 주세요."
        )
    elif stop_reason == "guardrail_intervened":
        result = (
            "요청이 Guardrail 안전 정책에 의해 차단되었습니다. "
            "질문을 바꿔 주세요."
        )
    elif stop_reason == "refusal":
        result = (
            "모델이 이 요청에 대한 응답을 거부했습니다. "
            "다른 모델로 시도하거나 질문을 바꿔 주세요."
        )
    elif not result:
        result = "답변을 찾지 못하였습니다."
    logger.info(f"result: {result}")

    if references:
        result += chat._format_references_markdown(references)

    if notification_queue is not None and chat.debug_mode == "Enable":
        chat.update_final_result(notification_queue, result)

    return result, image_url

