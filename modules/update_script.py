# update_script.py
import os
import shutil
import time
import subprocess
import sys
import json
import re

def load_jsonc_values(path):
    """Loads data from a .jsonc file, ignoring comments, and returns only key-value pairs."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
        content = re.sub(r'//.*', '', content)
        content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
        return json.loads(content)
    except (FileNotFoundError, json.JSONDecodeError, Exception) as e:
        print(f"Error loading or parsing values from {path}: {e}")
        return None

def get_all_relative_paths(directory):
    """Gets a set of relative paths for all files and empty folders within a directory."""
    paths = set()
    for root, dirs, files in os.walk(directory):
        # Add files
        for name in files:
            path = os.path.join(root, name)
            paths.add(os.path.relpath(path, directory))
        # Add empty folders
        for name in dirs:
            dir_path = os.path.join(root, name)
            if not os.listdir(dir_path):
                paths.add(os.path.relpath(dir_path, directory) + os.sep)
    return paths

def main():
    print("--- Update script started ---")
    
    # 1. Wait for the main program to exit
    print("Waiting for the main program to shut down (3 seconds)...")
    time.sleep(3)
    
    # 2. Define paths
    destination_dir = os.getcwd()
    update_dir = "update_temp"
    source_dir_inner = os.path.join(update_dir, "LMArenaBridge-main")
    config_filename = 'config.jsonc'
    models_filename = 'models.json'
    model_endpoint_map_filename = 'model_endpoint_map.json'
    
    if not os.path.exists(source_dir_inner):
        print(f"Error: Source directory {source_dir_inner} not found. Update failed.")
        return
        
    print(f"Source directory: {os.path.abspath(source_dir_inner)}")
    print(f"Destination directory: {os.path.abspath(destination_dir)}")

    # 3. Back up key files
    print("Backing up current configuration and model files...")
    old_config_path = os.path.join(destination_dir, config_filename)
    old_models_path = os.path.join(destination_dir, models_filename)
    old_config_values = load_jsonc_values(old_config_path)
    
    # 4. Determine which files and folders to keep
    # Keep update_temp itself, the .git directory, and any hidden files/folders the user might have added
    preserved_items = {update_dir, ".git", ".github"}

    # 5. Get new and old file lists
    new_files = get_all_relative_paths(source_dir_inner)
    # Exclude .git and .github directories as they should not be deployed
    new_files = {f for f in new_files if not (f.startswith('.git') or f.startswith('.github'))}

    current_files = get_all_relative_paths(destination_dir)

    print("\n--- File Change Analysis ---")
    print("[*] File deletion is disabled to protect user data. Only file copying and configuration updates will be performed.")

    # 7. Copy new files (excluding configuration files)
    print("\n[+] Copying new files...")
    try:
        new_config_template_path = os.path.join(source_dir_inner, config_filename)
        
        for item in os.listdir(source_dir_inner):
            s = os.path.join(source_dir_inner, item)
            d = os.path.join(destination_dir, item)
            
            # Skip .git and .github directories
            if item in {".git", ".github"}:
                continue
            
            if os.path.basename(s) == config_filename:
                continue # Skip the main configuration file, it will be handled later
            
            if os.path.basename(s) == model_endpoint_map_filename:
                continue # Skip the model endpoint map file to keep the user's local version

            if os.path.basename(s) == models_filename:
                continue # Skip the models.json file to keep the user's local version

            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)
        print("Files copied successfully.")

    except Exception as e:
        print(f"An error occurred during file copying: {e}")
        return

    # 8. Smart-merge configuration
    if old_config_values and os.path.exists(new_config_template_path):
        print("\n[*] Smart-merging configuration (preserving comments)...")
        try:
            with open(new_config_template_path, 'r', encoding='utf-8') as f:
                new_config_content = f.read()

            new_version_values = load_jsonc_values(new_config_template_path)
            new_version = new_version_values.get("version", "unknown")
            old_config_values["version"] = new_version

            for key, value in old_config_values.items():
                if isinstance(value, str):
                    replacement_value = f'"{value}"'
                elif isinstance(value, bool):
                    replacement_value = str(value).lower()
                else:
                    replacement_value = str(value)
                
                pattern = re.compile(f'("{key}"\s*:\s*)(?:".*?"|true|false|[\d\.]+)')
                if pattern.search(new_config_content):
                    new_config_content = pattern.sub(f'\\g<1>{replacement_value}', new_config_content)

            with open(old_config_path, 'w', encoding='utf-8') as f:
                f.write(new_config_content)
            print("Configuration merged successfully.")

        except Exception as e:
            print(f"A critical error occurred during configuration merge: {e}")
    else:
        print("Could not perform a smart-merge. The new configuration file will be used directly.")
        if os.path.exists(new_config_template_path):
            shutil.copy2(new_config_template_path, old_config_path)

    # 9. Clean up the temporary folder
    print("\n[*] Cleaning up temporary files...")
    try:
        shutil.rmtree(update_dir)
        print("Cleanup complete.")
    except Exception as e:
        print(f"An error occurred while cleaning up temporary files: {e}")

    # 10. Restart the main program
    print("\n[*] Restarting the main program...")
    try:
        main_script_path = os.path.join(destination_dir, "api_server.py")
        if not os.path.exists(main_script_path):
             print(f"Error: Main program script {main_script_path} not found.")
             return
        
        subprocess.Popen([sys.executable, main_script_path])
        print("The main program has been restarted in the background.")
    except Exception as e:
        print(f"Failed to restart the main program: {e}")
        print(f"Please run {main_script_path} manually")

    print("--- Update complete ---")

if __name__ == "__main__":
    main()