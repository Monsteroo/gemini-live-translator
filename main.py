import os
import asyncio
from config import load_env
from livetranslate_client import LiveTranslateClient

load_env()

def print_banner():
    print("=" * 60)
    print("  Powered by Qwen qwen3.5-livetranslate-flash-realtime")
    print("=" * 60 + "\n")

def get_user_config():
    """Get user configuration."""
    print("Select a mode:")
    print("1. Voice + Text [Default] | 2. Text Only")
    mode_choice = input("Enter your choice (press Enter for Voice + Text): ").strip()
    audio_enabled = (mode_choice != "2")

    if audio_enabled:
        lang_map = {
            "1": "en", "2": "zh", "3": "ru", "4": "fr", "5": "de", "6": "pt",
            "7": "es", "8": "it", "9": "ko", "10": "ja", "11": "yue"
        }
        print("Select the target language (Voice + Text mode):")
        print("1. English | 2. Chinese | 3. Russian | 4. French | 5. German | 6. Portuguese | 7. Spanish | 8. Italian | 9. Korean | 10. Japanese | 11. Cantonese")
    else:
        lang_map = {
            "1": "en", "2": "zh", "3": "ru", "4": "fr", "5": "de", "6": "pt", "7": "es", "8": "it",
            "9": "id", "10": "ko", "11": "ja", "12": "vi", "13": "th", "14": "ar",
            "15": "yue", "16": "hi", "17": "el", "18": "tr"
        }
        print("Select the target language (Text Only mode):")
        print("1. English | 2. Chinese | 3. Russian | 4. French | 5. German | 6. Portuguese | 7. Spanish | 8. Italian | 9. Indonesian | 10. Korean | 11. Japanese | 12. Vietnamese | 13. Thai | 14. Arabic | 15. Cantonese | 16. Hindi | 17. Greek | 18. Turkish")

    choice = input("Enter your choice (defaults to the first option): ").strip()
    target_language = lang_map.get(choice, next(iter(lang_map.values())))

    return target_language, audio_enabled

async def main():
    """Main program entry point."""
    print_banner()
    
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        print("[ERROR] Please set the DASHSCOPE_API_KEY environment variable.")
        print("  For example: export DASHSCOPE_API_KEY='your_api_key_here'")
        return
        
    target_language, audio_enabled = get_user_config()
    print("\nConfiguration complete:")
    print(f"  - Target language: {target_language}")
    if not audio_enabled:
        print("  - Output mode: Text Only")

    client = LiveTranslateClient(api_key=api_key, target_language=target_language, audio_enabled=audio_enabled)
    
    # Define the callback function.
    def on_translation_text(text):
        print(text, end="", flush=True)

    try:
        print("Connecting to the translation service...")
        await client.connect()
        
        # Start audio playback based on the mode.
        client.start_audio_player()
        
        print("\n" + "-" * 60)
        print("Connection successful! Speak into the microphone.")
        print("The program will translate your speech in real time and play the translated audio. Press Ctrl+C to exit.")
        print("-" * 60 + "\n")

        # Run message handling and microphone recording concurrently.
        message_handler = asyncio.create_task(client.handle_server_messages(on_translation_text))
        tasks = [message_handler]
        # Capture audio from the microphone for translation, regardless of whether audio output is enabled.
        microphone_streamer = asyncio.create_task(client.start_microphone_streaming())
        tasks.append(microphone_streamer)

        await asyncio.gather(*tasks)

    except KeyboardInterrupt:
        print("\n\nUser interrupted. Exiting...")
    except Exception as e:
        print(f"\nA critical error occurred: {e}")
    finally:
        print("\nCleaning up resources...")
        await client.close()
        print("Program exited.")

if __name__ == "__main__":
    asyncio.run(main())