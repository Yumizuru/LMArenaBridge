import asyncio
import json
import logging
import os
import sys
import subprocess
import time
import uuid
import re
import threading
import random
import mimetypes
from datetime import datetime
from contextlib import asynccontextmanager

import uvicorn
import requests
from packaging.version import parse as parse_version
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, Response


# --- Basic Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Global State and Configuration ---
CONFIG = {} # Stores the configuration loaded from config.jsonc
# browser_ws is used to store the WebSocket connection with a single Tampermonkey script.
# Note: This architecture assumes only one browser tab is active.
# If multiple concurrent tabs need to be supported, this needs to be expanded into a dictionary to manage multiple connections.
browser_ws: WebSocket | None = None
# response_channels is used to store the response queue for each API request.
# The key is request_id, and the value is asyncio.Queue.
response_channels: dict[str, asyncio.Queue] = {}
last_activity_time = None # Records the time of the last activity
idle_monitor_thread = None # Idle monitor thread
main_event_loop = None # Main event loop

# --- Model Mapping ---
# MODEL_NAME_TO_ID_MAP will now store richer objects: { "model_name": {"id": "...", "type": "..."} }
MODEL_NAME_TO_ID_MAP = {}
MODEL_ENDPOINT_MAP = {} # New: Used to store the mapping from model to session/message ID
DEFAULT_MODEL_ID = None # Default model id: None

def load_model_endpoint_map():
    """Loads the model-to-endpoint mapping from model_endpoint_map.json."""
    global MODEL_ENDPOINT_MAP
    try:
        with open('model_endpoint_map.json', 'r', encoding='utf-8') as f:
            content = f.read()
            # Allow empty file
            if not content.strip():
                MODEL_ENDPOINT_MAP = {}
            else:
                MODEL_ENDPOINT_MAP = json.loads(content)
        logger.info(f"Successfully loaded {len(MODEL_ENDPOINT_MAP)} model endpoint mappings from 'model_endpoint_map.json'.")
    except FileNotFoundError:
        logger.warning("'model_endpoint_map.json' file not found. An empty map will be used.")
        MODEL_ENDPOINT_MAP = {}
    except json.JSONDecodeError as e:
        logger.error(f"Failed to load or parse 'model_endpoint_map.json': {e}. An empty map will be used.")
        MODEL_ENDPOINT_MAP = {}

def load_config():
    """Loads configuration from config.jsonc and handles JSONC comments."""
    global CONFIG
    try:
        with open('config.jsonc', 'r', encoding='utf-8') as f:
            content = f.read()
            # Remove // line comments and /* */ block comments
            json_content = re.sub(r'//.*', '', content)
            json_content = re.sub(r'/\*.*?\*/', '', json_content, flags=re.DOTALL)
            CONFIG = json.loads(json_content)
        logger.info("Successfully loaded configuration from 'config.jsonc'.")
        # Print key configuration status
        logger.info(f"  - Tavern Mode: {'✅ Enabled' if CONFIG.get('tavern_mode_enabled') else '❌ Disabled'}")
        logger.info(f"  - Bypass Mode: {'✅ Enabled' if CONFIG.get('bypass_enabled') else '❌ Disabled'}")
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error(f"Failed to load or parse 'config.jsonc': {e}. Default configuration will be used.")
        CONFIG = {}

def load_model_map():
    """Loads model mapping from models.json, supports 'id:type' format."""
    global MODEL_NAME_TO_ID_MAP
    try:
        with open('models.json', 'r', encoding='utf-8') as f:
            raw_map = json.load(f)
            
        processed_map = {}
        for name, value in raw_map.items():
            if isinstance(value, str) and ':' in value:
                parts = value.split(':', 1)
                model_id = parts[0] if parts[0].lower() != 'null' else None
                model_type = parts[1]
                processed_map[name] = {"id": model_id, "type": model_type}
            else:
                # Default or old format handling
                processed_map[name] = {"id": value, "type": "text"}

        MODEL_NAME_TO_ID_MAP = processed_map
        logger.info(f"Successfully loaded and parsed {len(MODEL_NAME_TO_ID_MAP)} models from 'models.json'.")

    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error(f"Failed to load 'models.json': {e}. An empty model list will be used.")
        MODEL_NAME_TO_ID_MAP = {}

# --- Update Check ---
GITHUB_REPO = "Lianues/LMArenaBridge"

def download_and_extract_update(version):
    """Downloads and extracts the latest version to a temporary folder."""
    update_dir = "update_temp"
    if not os.path.exists(update_dir):
        os.makedirs(update_dir)

    try:
        zip_url = f"https://github.com/{GITHUB_REPO}/archive/refs/heads/main.zip"
        logger.info(f"Downloading new version from {zip_url}...")
        response = requests.get(zip_url, timeout=60)
        response.raise_for_status()

        # Need to import zipfile and io
        import zipfile
        import io
        with zipfile.ZipFile(io.BytesIO(response.content)) as z:
            z.extractall(update_dir)
        
        logger.info(f"New version successfully downloaded and extracted to '{update_dir}' folder.")
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to download update: {e}")
    except zipfile.BadZipFile:
        logger.error("The downloaded file is not a valid zip archive.")
    except Exception as e:
        logger.error(f"An unknown error occurred while extracting the update: {e}")
    
    return False

def check_for_updates():
    """Checks for new versions from GitHub."""
    if not CONFIG.get("enable_auto_update", True):
        logger.info("Auto-update is disabled, skipping check.")
        return

    current_version = CONFIG.get("version", "0.0.0")
    logger.info(f"Current version: {current_version}. Checking for updates from GitHub...")

    try:
        config_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/config.jsonc"
        response = requests.get(config_url, timeout=10)
        response.raise_for_status()

        jsonc_content = response.text
        json_content = re.sub(r'//.*', '', jsonc_content)
        json_content = re.sub(r'/\*.*?\*/', '', json_content, flags=re.DOTALL)
        remote_config = json.loads(json_content)
        
        remote_version_str = remote_config.get("version")
        if not remote_version_str:
            logger.warning("Version number not found in the remote configuration file, skipping update check.")
            return

        if parse_version(remote_version_str) > parse_version(current_version):
            logger.info("="*60)
            logger.info(f"🎉 New version found! 🎉")
            logger.info(f"  - Current version: {current_version}")
            logger.info(f"  - Latest version: {remote_version_str}")
            if download_and_extract_update(remote_version_str):
                logger.info("Preparing to apply the update. The server will shut down in 5 seconds and start the update script.")
                time.sleep(5)
                update_script_path = os.path.join("modules", "update_script.py")
                # Use Popen to start an independent process
                subprocess.Popen([sys.executable, update_script_path])
                # Gracefully exit the current server process
                os._exit(0)
            else:
                logger.error(f"Automatic update failed. Please visit https://github.com/{GITHUB_REPO}/releases/latest to download manually.")
            logger.info("="*60)
        else:
            logger.info("Your program is already the latest version.")

    except requests.RequestException as e:
        logger.error(f"Failed to check for updates: {e}")
    except json.JSONDecodeError:
        logger.error("Failed to parse the remote configuration file.")
    except Exception as e:
        logger.error(f"An unknown error occurred while checking for updates: {e}")

# --- Model Update ---
def extract_models_from_html(html_content):
    """
    Extracts complete model JSON objects from HTML content, using bracket matching to ensure integrity.
    """
    models = []
    model_names = set()
    
    # Find all possible starting positions of model JSON objects
    for start_match in re.finditer(r'\{\\"id\\":\\"[a-f0-9-]+\\"', html_content):
        start_index = start_match.start()
        
        # Start brace matching from the starting position
        open_braces = 0
        end_index = -1
        
        # Optimization: Set a reasonable search limit to avoid infinite loops
        search_limit = start_index + 10000 # Assume a model definition will not exceed 10000 characters
        
        for i in range(start_index, min(len(html_content), search_limit)):
            if html_content[i] == '{':
                open_braces += 1
            elif html_content[i] == '}':
                open_braces -= 1
                if open_braces == 0:
                    end_index = i + 1
                    break
        
        if end_index != -1:
            # Extract the complete, escaped JSON string
            json_string_escaped = html_content[start_index:end_index]
            
            # Unescape
            json_string = json_string_escaped.replace('\\"', '"').replace('\\\\', '\\')
            
            try:
                model_data = json.loads(json_string)
                model_name = model_data.get('publicName')
                
                # Deduplicate using publicName
                if model_name and model_name not in model_names:
                    models.append(model_data)
                    model_names.add(model_name)
            except json.JSONDecodeError as e:
                logger.warning(f"Error parsing extracted JSON object: {e} - Content: {json_string[:150]}...")
                continue

    if models:
        logger.info(f"Successfully extracted and parsed {len(models)} unique models.")
        return models
    else:
        logger.error("Error: No matching complete model JSON objects found in the HTML response.")
        return None

def save_available_models(new_models_list, models_path="available_models.json"):
    """
    Saves the list of extracted complete model objects to the specified JSON file.
    """
    logger.info(f"Detected {len(new_models_list)} models, updating '{models_path}'...")
    
    try:
        with open(models_path, 'w', encoding='utf-8') as f:
            # Directly write the complete list of model objects to the file
            json.dump(new_models_list, f, indent=4, ensure_ascii=False)
        logger.info(f"✅ '{models_path}' has been successfully updated with {len(new_models_list)} models.")
    except IOError as e:
        logger.error(f"❌ Error writing to '{models_path}' file: {e}")

# --- Auto-Restart Logic ---
def restart_server():
    """Gracefully notifies the client to refresh, then restarts the server."""
    logger.warning("="*60)
    logger.warning("Idle timeout detected, preparing to auto-restart the server...")
    logger.warning("="*60)
    
    # 1. (Asynchronously) notify the browser to refresh
    async def notify_browser_refresh():
        if browser_ws:
            try:
                # Prioritize sending 'reconnect' command to let the frontend know this is a planned restart
                await browser_ws.send_text(json.dumps({"command": "reconnect"}, ensure_ascii=False))
                logger.info("Sent 'reconnect' command to the browser.")
            except Exception as e:
                logger.error(f"Failed to send 'reconnect' command: {e}")
    
    # Run the async notification function in the main event loop
    # Use `asyncio.run_coroutine_threadsafe` to ensure thread safety
    if browser_ws and browser_ws.client_state.name == 'CONNECTED' and main_event_loop:
        asyncio.run_coroutine_threadsafe(notify_browser_refresh(), main_event_loop)
    
    # 2. Delay a few seconds to ensure the message is sent
    time.sleep(3)
    
    # 3. Execute the restart
    logger.info("Restarting server...")
    os.execv(sys.executable, ['python'] + sys.argv)

def idle_monitor():
    """Runs in a background thread to monitor if the server is idle."""
    global last_activity_time
    
    # Wait until last_activity_time is set for the first time
    while last_activity_time is None:
        time.sleep(1)
        
    logger.info("Idle monitor thread has started.")
    
    while True:
        if CONFIG.get("enable_idle_restart", False):
            timeout = CONFIG.get("idle_restart_timeout_seconds", 300)
            
            # If timeout is set to -1, disable the restart check
            if timeout == -1:
                time.sleep(10) # Still need to sleep to avoid a busy loop
                continue

            idle_time = (datetime.now() - last_activity_time).total_seconds()
            
            if idle_time > timeout:
                logger.info(f"Server idle time ({idle_time:.0f}s) has exceeded the threshold ({timeout}s).")
                restart_server()
                break # Exit the loop as the process is about to be replaced
                
        # Check every 10 seconds
        time.sleep(10)

# --- FastAPI Lifespan Events ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan function that runs on server startup."""
    global idle_monitor_thread, last_activity_time, main_event_loop
    main_event_loop = asyncio.get_running_loop() # Get the main event loop
    load_config() # First, load the configuration
    
    # --- Print the current operation mode ---
    mode = CONFIG.get("id_updater_last_mode", "direct_chat")
    target = CONFIG.get("id_updater_battle_target", "A")
    logger.info("="*60)
    logger.info(f"  Current operation mode: {mode.upper()}")
    if mode == 'battle':
        logger.info(f"  - Battle mode target: Assistant {target}")
    logger.info("  (Mode can be changed by running id_updater.py)")
    logger.info("="*60)

    check_for_updates() # Check for program updates
    load_model_map() # Re-enable model loading
    load_model_endpoint_map() # Load model endpoint mappings
    logger.info("Server startup complete. Waiting for Tampermonkey script connection...")

    # Mark the starting point of activity time after model updates
    last_activity_time = datetime.now()
    
    # Start the idle monitor thread
    if CONFIG.get("enable_idle_restart", False):
        idle_monitor_thread = threading.Thread(target=idle_monitor, daemon=True)
        idle_monitor_thread.start()
        
    yield
    logger.info("Server is shutting down.")

app = FastAPI(lifespan=lifespan)

# --- CORS Middleware Configuration ---
# Allow all origins, all methods, all headers, which is safe for local development tools.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Helper Functions ---
def save_config():
    """Writes the current CONFIG object back to the config.jsonc file, preserving comments."""
    try:
        # Read the original file to preserve comments, etc.
        with open('config.jsonc', 'r', encoding='utf-8') as f:
            lines = f.readlines()

        # Safely replace values using regular expressions
        def replacer(key, value, content):
            # This regex finds the key, then matches its value part until a comma or a closing brace
            pattern = re.compile(rf'("{key}"\s*:\s*").*?("?)(,?\s*)$', re.MULTILINE)
            replacement = rf'\g<1>{value}\g<2>\g<3>'
            if not pattern.search(content): # If the key doesn't exist, add it to the end of the file (simplified handling)
                 content = re.sub(r'}\s*$', f'  ,"{key}": "{value}"\n}}', content)
            else:
                 content = pattern.sub(replacement, content)
            return content

        content_str = "".join(lines)
        content_str = replacer("session_id", CONFIG["session_id"], content_str)
        content_str = replacer("message_id", CONFIG["message_id"], content_str)
        
        with open('config.jsonc', 'w', encoding='utf-8') as f:
            f.write(content_str)
        logger.info("✅ Successfully updated session information in config.jsonc.")
    except Exception as e:
        logger.error(f"❌ Error writing to config.jsonc: {e}", exc_info=True)


def _process_openai_message(message: dict) -> dict:
    """
    Processes an OpenAI message, separating text and attachments.
    - Decomposes a multimodal content list into plain text and an attachments list.
    - Ensures that empty content for the 'user' role is replaced with a space to avoid errors in LMArena.
    - Generates a basic structure for attachments.
    """
    content = message.get("content")
    role = message.get("role")
    attachments = []
    text_content = ""

    if isinstance(content, list):
        
        text_parts = []
        for part in content:
            if part.get("type") == "text":
                text_parts.append(part.get("text", ""))
            elif part.get("type") == "image_url":
                image_url_data = part.get("image_url", {})
                url = image_url_data.get("url")

                # New logic: Allow the client to pass the original filename via the 'detail' field
                # The 'detail' field is part of the OpenAI Vision API, we reuse it here
                original_filename = image_url_data.get("detail")

                if url and url.startswith("data:"):
                    try:
                        content_type = url.split(';')[0].split(':')[1]
                        
                        # If the client provides an original filename, use it directly
                        if original_filename and isinstance(original_filename, str):
                            file_name = original_filename
                            logger.info(f"Successfully processed an attachment (using original filename): {file_name}")
                        else:
                            # Otherwise, fall back to the old UUID-based naming logic
                            main_type, sub_type = content_type.split('/') if '/' in content_type else ('application', 'octet-stream')
                            
                            if main_type == "image": prefix = "image"
                            elif main_type == "audio": prefix = "audio"
                            else: prefix = "file"
                            
                            guessed_extension = mimetypes.guess_extension(content_type)
                            if guessed_extension:
                                file_extension = guessed_extension.lstrip('.')
                            else:
                                file_extension = sub_type if len(sub_type) < 20 else 'bin'
                            
                            file_name = f"{prefix}_{uuid.uuid4()}.{file_extension}"
                            logger.info(f"Successfully processed an attachment (generating filename): {file_name}")

                        attachments.append({
                            "name": file_name,
                            "contentType": content_type,
                            "url": url
                        })
                    except (IndexError, ValueError) as e:
                        logger.warning(f"Could not parse base64 data URI: {url[:60]}... Error: {e}")

        text_content = "\n\n".join(text_parts)
    elif isinstance(content, str):
        text_content = content

    
    if role == "user" and not text_content.strip():
        text_content = " "

    return {
        "role": role,
        "content": text_content,
        "attachments": attachments
    }

def convert_openai_to_lmarena_payload(openai_data: dict, session_id: str, message_id: str, mode_override: str = None, battle_target_override: str = None) -> dict:
    """
    Converts an OpenAI request body to the simplified payload required by the Tampermonkey script,
    and applies Tavern Mode, Bypass Mode, and Battle Mode.
    Adds mode override parameters to support model-specific session modes.
    """
    # 1. Normalize roles and process messages
    #    - Converts non-standard 'developer' role to 'system' for better compatibility.
    #    - Separates text and attachments.
    messages = openai_data.get("messages", [])
    for msg in messages:
        if msg.get("role") == "developer":
            msg["role"] = "system"
            logger.info("Message role normalized: 'developer' converted to 'system'.")
            
    processed_messages = [_process_openai_message(msg.copy()) for msg in messages]

    # 2. Apply Tavern Mode
    if CONFIG.get("tavern_mode_enabled"):
        system_prompts = [msg['content'] for msg in processed_messages if msg['role'] == 'system']
        other_messages = [msg for msg in processed_messages if msg['role'] != 'system']
        
        merged_system_prompt = "\n\n".join(system_prompts)
        final_messages = []
        
        if merged_system_prompt:
            # System messages should not have attachments
            final_messages.append({"role": "system", "content": merged_system_prompt, "attachments": []})
        
        final_messages.extend(other_messages)
        processed_messages = final_messages

    # 3. Determine the target model ID
    model_name = openai_data.get("model", "claude-3-5-sonnet-20241022")
    model_info = MODEL_NAME_TO_ID_MAP.get(model_name, {}) # Critical fix: ensure model_info is always a dictionary
    
    target_model_id = None
    if model_info:
        target_model_id = model_info.get("id")
    else:
        logger.warning(f"Model '{model_name}' not found in 'models.json'. The request will be sent without a specific model ID.")

    if not target_model_id:
        logger.warning(f"No corresponding ID found for model '{model_name}' in 'models.json'. The request will be sent without a specific model ID.")

    # 4. Build message templates
    message_templates = []
    for msg in processed_messages:
        message_templates.append({
            "role": msg["role"],
            "content": msg.get("content", ""),
            "attachments": msg.get("attachments", [])
        })

    # 5. Apply Bypass Mode - only effective for text models
    model_type = model_info.get("type", "text")
    if CONFIG.get("bypass_enabled") and model_type == "text":
        # Bypass mode always adds a user message with position 'a'
        logger.info("Bypass mode enabled, injecting an empty user message.")
        message_templates.append({"role": "user", "content": " ", "participantPosition": "a", "attachments": []})

    # 6. Apply Participant Position
    # Prioritize the override mode, otherwise fall back to the global configuration
    mode = mode_override or CONFIG.get("id_updater_last_mode", "direct_chat")
    target_participant = battle_target_override or CONFIG.get("id_updater_battle_target", "A")
    target_participant = target_participant.lower() # Ensure it's lowercase

    logger.info(f"Setting Participant Positions based on mode '{mode}' (target: {target_participant if mode == 'battle' else 'N/A'})...")

    for msg in message_templates:
        if msg['role'] == 'system':
            if mode == 'battle':
                # Battle Mode: system is on the same side as the user-selected assistant (A -> a, B -> b)
                msg['participantPosition'] = target_participant
            else:
                # DirectChat Mode: system is fixed to 'b'
                msg['participantPosition'] = 'b'
        elif mode == 'battle':
            # In Battle Mode, non-system messages use the user-selected target participant
            msg['participantPosition'] = target_participant
        else: # DirectChat Mode
            # In DirectChat Mode, non-system messages use the default 'a'
            msg['participantPosition'] = 'a'

    return {
        "message_templates": message_templates,
        "target_model_id": target_model_id,
        "session_id": session_id,
        "message_id": message_id
    }

# --- OpenAI Formatting Helper Functions (ensuring robust JSON serialization) ---
def format_openai_chunk(content: str, model: str, request_id: str) -> str:
    """Formats into an OpenAI streaming chunk."""
    chunk = {
        "id": request_id, "object": "chat.completion.chunk",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

def format_openai_finish_chunk(model: str, request_id: str, reason: str = 'stop') -> str:
    """Formats into an OpenAI finish chunk."""
    chunk = {
        "id": request_id, "object": "chat.completion.chunk",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\ndata: [DONE]\n\n"

def format_openai_error_chunk(error_message: str, model: str, request_id: str) -> str:
    """Formats into an OpenAI error chunk."""
    content = f"\n\n[LMArena Bridge Error]: {error_message}"
    return format_openai_chunk(content, model, request_id)

def format_openai_non_stream_response(content: str, model: str, request_id: str, reason: str = 'stop') -> dict:
    """Builds an OpenAI-compliant non-streaming response body."""
    return {
        "id": request_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": reason,
        }],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": len(content) // 4,
            "total_tokens": len(content) // 4,
        },
    }

async def _process_lmarena_stream(request_id: str):
    """
    Core internal generator: processes the raw data stream from the browser and yields structured events.
    Event types: ('content', str), ('finish', str), ('error', str)
    """
    queue = response_channels.get(request_id)
    if not queue:
        logger.error(f"PROCESSOR [ID: {request_id[:8]}]: Could not find the response channel.")
        yield 'error', 'Internal server error: response channel not found.'
        return

    buffer = ""
    timeout = CONFIG.get("stream_response_timeout_seconds", 360)
    text_pattern = re.compile(r'[ab]0:"((?:\\.|[^"\\])*)"')
    # New: Regex for matching and extracting image URLs
    image_pattern = re.compile(r'[ab]2:(\[.*?\])')
    finish_pattern = re.compile(r'[ab]d:(\{.*?"finishReason".*?\})')
    error_pattern = re.compile(r'(\{\s*"error".*?\})', re.DOTALL)
    cloudflare_patterns = [r'<title>Just a moment...</title>', r'Enable JavaScript and cookies to continue']

    try:
        while True:
            try:
                raw_data = await asyncio.wait_for(queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(f"PROCESSOR [ID: {request_id[:8]}]: Timed out waiting for browser data ({timeout} seconds).")
                yield 'error', f'Response timed out after {timeout} seconds.'
                return

            # 1. Check for direct error or termination signals from the WebSocket side
            if isinstance(raw_data, dict) and 'error' in raw_data:
                error_msg = raw_data.get('error', 'Unknown browser error')
                
                # Enhanced error handling
                if isinstance(error_msg, str):
                    # 1. Check for 413 attachment too large error
                    if '413' in error_msg or 'too large' in error_msg.lower():
                        friendly_error_msg = "Upload failed: The attachment size exceeds the LMArena server's limit (usually around 5MB). Please try compressing the file or uploading a smaller one."
                        logger.warning(f"PROCESSOR [ID: {request_id[:8]}]: Detected attachment too large error (413).")
                        yield 'error', friendly_error_msg
                        return

                    # 2. Check for Cloudflare verification page
                    if any(re.search(p, error_msg, re.IGNORECASE) for p in cloudflare_patterns):
                        friendly_error_msg = "Cloudflare verification page detected. Please refresh the LMArena page in your browser and complete the verification manually, then retry the request."
                        if browser_ws:
                            try:
                                await browser_ws.send_text(json.dumps({"command": "refresh"}, ensure_ascii=False))
                                logger.info(f"PROCESSOR [ID: {request_id[:8]}]: Detected CF in error message and sent refresh command.")
                            except Exception as e:
                                logger.error(f"PROCESSOR [ID: {request_id[:8]}]: Failed to send refresh command: {e}")
                        yield 'error', friendly_error_msg
                        return

                # 3. Other unknown errors
                yield 'error', error_msg
                return
            if raw_data == "[DONE]":
                break

            buffer += "".join(str(item) for item in raw_data) if isinstance(raw_data, list) else raw_data

            if any(re.search(p, buffer, re.IGNORECASE) for p in cloudflare_patterns):
                error_msg = "Cloudflare verification page detected. Please refresh the LMArena page in your browser and complete the verification manually, then retry the request."
                if browser_ws:
                    try:
                        await browser_ws.send_text(json.dumps({"command": "refresh"}, ensure_ascii=False))
                        logger.info(f"PROCESSOR [ID: {request_id[:8]}]: Sent page refresh command to the browser.")
                    except Exception as e:
                        logger.error(f"PROCESSOR [ID: {request_id[:8]}]: Failed to send refresh command: {e}")
                yield 'error', error_msg
                return
            
            if (error_match := error_pattern.search(buffer)):
                try:
                    error_json = json.loads(error_match.group(1))
                    yield 'error', error_json.get("error", "Unknown error from LMArena")
                    return
                except json.JSONDecodeError: pass

            # Prioritize processing text content
            while (match := text_pattern.search(buffer)):
                try:
                    text_content = json.loads(f'"{match.group(1)}"')
                    if text_content: yield 'content', text_content
                except (ValueError, json.JSONDecodeError): pass
                buffer = buffer[match.end():]

            # New: Handle image content
            while (match := image_pattern.search(buffer)):
                try:
                    image_data_list = json.loads(match.group(1))
                    if isinstance(image_data_list, list) and image_data_list:
                        image_info = image_data_list[0]
                        if image_info.get("type") == "image" and "image" in image_info:
                            # Wrap the URL in Markdown format and yield it as a content chunk
                            markdown_image = f"![Image]({image_info['image']})"
                            yield 'content', markdown_image
                except (json.JSONDecodeError, IndexError) as e:
                    logger.warning(f"Error parsing image URL: {e}, buffer: {buffer[:150]}")
                buffer = buffer[match.end():]

            if (finish_match := finish_pattern.search(buffer)):
                try:
                    finish_data = json.loads(finish_match.group(1))
                    yield 'finish', finish_data.get("finishReason", "stop")
                except (json.JSONDecodeError, IndexError): pass
                buffer = buffer[finish_match.end():]

    except asyncio.CancelledError:
        logger.info(f"PROCESSOR [ID: {request_id[:8]}]: Task was cancelled.")
    finally:
        if request_id in response_channels:
            del response_channels[request_id]
            logger.info(f"PROCESSOR [ID: {request_id[:8]}]: Response channel has been cleaned up.")

async def stream_generator(request_id: str, model: str):
    """Formats the internal event stream into an OpenAI SSE response."""
    response_id = f"chatcmpl-{uuid.uuid4()}"
    logger.info(f"STREAMER [ID: {request_id[:8]}]: Stream generator started.")
    
    finish_reason_to_send = 'stop'  # Default finish reason

    async for event_type, data in _process_lmarena_stream(request_id):
        if event_type == 'content':
            yield format_openai_chunk(data, model, response_id)
        elif event_type == 'finish':
            # Record the finish reason, but don't return immediately; wait for [DONE] from the browser
            finish_reason_to_send = data
            if data == 'content-filter':
                warning_msg = "\n\nResponse terminated, possibly due to context length limit or internal model censorship (most likely)."
                yield format_openai_chunk(warning_msg, model, response_id)
        elif event_type == 'error':
            logger.error(f"STREAMER [ID: {request_id[:8]}]: An error occurred in the stream: {data}")
            yield format_openai_error_chunk(str(data), model, response_id)
            yield format_openai_finish_chunk(model, response_id, reason='stop')
            return # Can terminate immediately on error

    # Only execute after _process_lmarena_stream finishes naturally (i.e., receives [DONE])
    yield format_openai_finish_chunk(model, response_id, reason=finish_reason_to_send)
    logger.info(f"STREAMER [ID: {request_id[:8]}]: Stream generator finished normally.")

async def non_stream_response(request_id: str, model: str):
    """Aggregates the internal event stream and returns a single OpenAI JSON response."""
    response_id = f"chatcmpl-{uuid.uuid4()}"
    logger.info(f"NON-STREAM [ID: {request_id[:8]}]: Started processing non-stream response.")
    
    full_content = []
    finish_reason = "stop"
    
    async for event_type, data in _process_lmarena_stream(request_id):
        if event_type == 'content':
            full_content.append(data)
        elif event_type == 'finish':
            finish_reason = data
            if data == 'content-filter':
                full_content.append("\n\nResponse terminated, possibly due to context length limit or internal model censorship (most likely).")
            # Don't break here; continue waiting for the [DONE] signal from the browser to avoid race conditions
        elif event_type == 'error':
            logger.error(f"NON-STREAM [ID: {request_id[:8]}]: An error occurred during processing: {data}")
            
            # Unify error status codes for stream and non-stream responses
            status_code = 413 if "attachment size exceeds" in str(data) else 500

            error_response = {
                "error": {
                    "message": f"[LMArena Bridge Error]: {data}",
                    "type": "bridge_error",
                    "code": "attachment_too_large" if status_code == 413 else "processing_error"
                }
            }
            return Response(content=json.dumps(error_response, ensure_ascii=False), status_code=status_code, media_type="application/json")

    final_content = "".join(full_content)
    response_data = format_openai_non_stream_response(final_content, model, response_id, reason=finish_reason)
    
    logger.info(f"NON-STREAM [ID: {request_id[:8]}]: Response aggregation complete.")
    return Response(content=json.dumps(response_data, ensure_ascii=False), media_type="application/json")

# --- WebSocket Endpoint ---
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Handles WebSocket connections from the Tampermonkey script."""
    global browser_ws
    await websocket.accept()
    if browser_ws is not None:
        logger.warning("New Tampermonkey script connection detected, the old connection will be replaced.")
    logger.info("✅ Tampermonkey script successfully connected to WebSocket.")
    browser_ws = websocket
    try:
        while True:
            # Wait for and receive messages from the Tampermonkey script
            message_str = await websocket.receive_text()
            message = json.loads(message_str)
            
            request_id = message.get("request_id")
            data = message.get("data")

            if not request_id or data is None:
                logger.warning(f"Received invalid message from browser: {message}")
                continue

            # Put the received data into the corresponding response channel
            if request_id in response_channels:
                await response_channels[request_id].put(data)
            else:
                logger.warning(f"⚠️ Received response for an unknown or closed request: {request_id}")

    except WebSocketDisconnect:
        logger.warning("❌ Tampermonkey script client has disconnected.")
    except Exception as e:
        logger.error(f"An unknown error occurred during WebSocket handling: {e}", exc_info=True)
    finally:
        browser_ws = None
        # Clean up all waiting response channels to prevent requests from hanging
        for queue in response_channels.values():
            await queue.put({"error": "Browser disconnected during operation"})
        response_channels.clear()
        logger.info("WebSocket connection has been cleaned up.")

# --- OpenAI Compatible API Endpoints ---
@app.get("/v1/models")
async def get_models():
    """Provides an OpenAI-compatible model list."""
    if not MODEL_NAME_TO_ID_MAP:
        return JSONResponse(
            status_code=404,
            content={"error": "Model list is empty or 'models.json' was not found."}
        )
    
    return {
        "object": "list",
        "data": [
            {
                "id": model_name, 
                "object": "model",
                "created": int(time.time()),
                "owned_by": "LMArenaBridge"
            }
            for model_name in MODEL_NAME_TO_ID_MAP.keys()
        ],
    }

@app.post("/internal/request_model_update")
async def request_model_update():
    """
    Receives a request from model_updater.py and instructs the
    Tampermonkey script via WebSocket to send the page source.
    """
    if not browser_ws:
        logger.warning("MODEL UPDATE: Received update request, but no browser is connected.")
        raise HTTPException(status_code=503, detail="Browser client not connected.")
    
    try:
        logger.info("MODEL UPDATE: Received update request, sending command via WebSocket...")
        await browser_ws.send_text(json.dumps({"command": "send_page_source"}))
        logger.info("MODEL UPDATE: 'send_page_source' command sent successfully.")
        return JSONResponse({"status": "success", "message": "Request to send page source sent."})
    except Exception as e:
        logger.error(f"MODEL UPDATE: Error sending command: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to send command via WebSocket.")

@app.post("/internal/update_available_models")
async def update_available_models_endpoint(request: Request):
    """
    Receives page HTML from the Tampermonkey script, extracts, and updates available_models.json.
    """
    html_content = await request.body()
    if not html_content:
        logger.warning("Model update request received no HTML content.")
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "No HTML content received."}
        )
    
    logger.info("Received page content from Tampermonkey script, starting to extract available models...")
    new_models_list = extract_models_from_html(html_content.decode('utf-8'))
    
    if new_models_list:
        save_available_models(new_models_list)
        return JSONResponse({"status": "success", "message": "Available models file updated."})
    else:
        logger.error("Failed to extract model data from the HTML provided by the Tampermonkey script.")
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "Could not extract model data from HTML."}
        )


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """
    Handles chat completion requests.
    Receives a request in OpenAI format, converts it to LMArena format,
    sends it to the Tampermonkey script via WebSocket, and then streams back the result.
    """
    global last_activity_time
    last_activity_time = datetime.now() # Update activity time
    logger.info(f"API request received, activity time updated to: {last_activity_time.strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        openai_req = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON request body")

    model_name = openai_req.get("model")
    model_info = MODEL_NAME_TO_ID_MAP.get(model_name, {}) # Critical fix: return an empty dict instead of None if model is not found
    model_type = model_info.get("type", "text") # Default to text

    # --- New: Logic based on model type ---
    if model_type == 'image':
        logger.info(f"Detected model '{model_name}' is of type 'image', will be processed through the main chat interface.")
        # For image models, we no longer call a separate processor but reuse the main chat logic,
        # because _process_lmarena_stream can now handle image data.
        # This means image generation now natively supports streaming and non-streaming responses.
        pass # Continue with the common chat logic below
    # --- End of image generation logic ---

    # If it's not an image model, execute the normal text generation logic
    load_config()  # Load the latest configuration in real-time to ensure session IDs etc. are up-to-date
    # --- API Key Validation ---
    api_key = CONFIG.get("api_key")
    if api_key:
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            raise HTTPException(
                status_code=401,
                detail="API Key not provided. Please provide it in the Authorization header as 'Bearer YOUR_KEY'."
            )
        
        provided_key = auth_header.split(' ')[1]
        if provided_key != api_key:
            raise HTTPException(
                status_code=401,
                detail="The provided API Key is incorrect."
            )

    if not browser_ws:
        raise HTTPException(status_code=503, detail="Tampermonkey script client not connected. Please ensure the LMArena page is open and the script is active.")

    # --- Model to Session ID Mapping Logic ---
    session_id, message_id = None, None
    mode_override, battle_target_override = None, None

    if model_name and model_name in MODEL_ENDPOINT_MAP:
        mapping_entry = MODEL_ENDPOINT_MAP[model_name]
        selected_mapping = None

        if isinstance(mapping_entry, list) and mapping_entry:
            selected_mapping = random.choice(mapping_entry)
            logger.info(f"Randomly selected a mapping for model '{model_name}' from the ID list.")
        elif isinstance(mapping_entry, dict):
            selected_mapping = mapping_entry
            logger.info(f"Found a single endpoint mapping for model '{model_name}' (old format).")
        
        if selected_mapping:
            session_id = selected_mapping.get("session_id")
            message_id = selected_mapping.get("message_id")
            # Critical: also get the mode information
            mode_override = selected_mapping.get("mode") # Can be None
            battle_target_override = selected_mapping.get("battle_target") # Can be None
            log_msg = f"Will use Session ID: ...{session_id[-6:] if session_id else 'N/A'}"
            if mode_override:
                log_msg += f" (Mode: {mode_override}"
                if mode_override == 'battle':
                    log_msg += f", Target: {battle_target_override or 'A'}"
                log_msg += ")"
            logger.info(log_msg)

    # If session_id is still None after the above processing, enter the global fallback logic
    if not session_id:
        if CONFIG.get("use_default_ids_if_mapping_not_found", True):
            session_id = CONFIG.get("session_id")
            message_id = CONFIG.get("message_id")
            # When using global IDs, do not set mode overrides, let it use the global config
            mode_override, battle_target_override = None, None
            logger.info(f"No valid mapping found for model '{model_name}', using global default Session ID as per configuration: ...{session_id[-6:] if session_id else 'N/A'}")
        else:
            logger.error(f"No valid mapping found for model '{model_name}' in 'model_endpoint_map.json', and fallback to default ID is disabled.")
            raise HTTPException(
                status_code=400,
                detail=f"Model '{model_name}' does not have a configured independent session ID. Please add a valid mapping in 'model_endpoint_map.json' or enable 'use_default_ids_if_mapping_not_found' in 'config.jsonc'."
            )

    # --- Validate the finally determined session information ---
    if not session_id or not message_id or "YOUR_" in session_id or "YOUR_" in message_id:
        raise HTTPException(
            status_code=400,
            detail="The finally determined session ID or message ID is invalid. Please check the configurations in 'model_endpoint_map.json' and 'config.jsonc', or run `id_updater.py` to update the default values."
        )

    if not model_name or model_name not in MODEL_NAME_TO_ID_MAP:
        logger.warning(f"The requested model '{model_name}' is not in models.json, will use the default model ID.")

    request_id = str(uuid.uuid4())
    response_channels[request_id] = asyncio.Queue()
    logger.info(f"API CALL [ID: {request_id[:8]}]: Response channel created.")

    try:
        # 1. Convert the request, passing in possible mode override information
        lmarena_payload = convert_openai_to_lmarena_payload(
            openai_req,
            session_id,
            message_id,
            mode_override=mode_override,
            battle_target_override=battle_target_override
        )
        
        # 2. Wrap it into a message to be sent to the browser
        message_to_browser = {
            "request_id": request_id,
            "payload": lmarena_payload
        }
        
        # 3. Send via WebSocket
        logger.info(f"API CALL [ID: {request_id[:8]}]: Sending payload to Tampermonkey script via WebSocket.")
        await browser_ws.send_text(json.dumps(message_to_browser))

        # 4. Decide the return type based on the 'stream' parameter
        is_stream = openai_req.get("stream", False)

        if is_stream:
            # Return a streaming response
            return StreamingResponse(
                stream_generator(request_id, model_name or "default_model"),
                media_type="text/event-stream"
            )
        else:
            # Return a non-streaming response
            return await non_stream_response(request_id, model_name or "default_model")
    except Exception as e:
        # If an error occurs during setup, clean up the channel
        if request_id in response_channels:
            del response_channels[request_id]
        logger.error(f"API CALL [ID: {request_id[:8]}]: A fatal error occurred while processing the request: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# --- Internal Communication Endpoints ---
@app.post("/internal/start_id_capture")
async def start_id_capture():
    """
    Receives a notification from id_updater.py and activates
    the Tampermonkey script's ID capture mode via WebSocket command.
    """
    if not browser_ws:
        logger.warning("ID CAPTURE: Received activation request, but no browser is connected.")
        raise HTTPException(status_code=503, detail="Browser client not connected.")
    
    try:
        logger.info("ID CAPTURE: Received activation request, sending command via WebSocket...")
        await browser_ws.send_text(json.dumps({"command": "activate_id_capture"}))
        logger.info("ID CAPTURE: Activation command sent successfully.")
        return JSONResponse({"status": "success", "message": "Activation command sent."})
    except Exception as e:
        logger.error(f"ID CAPTURE: Error sending activation command: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to send command via WebSocket.")


# --- Main Program Entry Point ---
if __name__ == "__main__":
    # It's recommended to read the port from config.jsonc, this is a temporary hardcoding
    api_port = 5102
    logger.info(f"🚀 LMArena Bridge v2.0 API Server is starting...")
    logger.info(f"   - Listening on: http://127.0.0.1:{api_port}")
    logger.info(f"   - WebSocket endpoint: ws://127.0.0.1:{api_port}/ws")
    
    uvicorn.run(app, host="0.0.0.0", port=api_port)