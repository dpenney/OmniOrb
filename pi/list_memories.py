import os
import sys

# Ensure we are in the right directory to find config
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import config
from mem0 import Memory
from dotenv import load_dotenv

# Load secret environment variables from .env
load_dotenv()

# Map GEMINI_API_KEY to GOOGLE_API_KEY for libraries that expect it
if os.getenv("GEMINI_API_KEY") and not os.getenv("GOOGLE_API_KEY"):
    os.environ["GOOGLE_API_KEY"] = os.getenv("GEMINI_API_KEY")

# Force Google AI API version to v1
os.environ["GOOGLE_API_VERSION"] = "v1"

def list_memories():
    mem_config = {
        "llm": {
            "provider": "gemini",
            "config": {
                "model": config.LLM_MODEL,
            }
        },
        "embedder": {
            "provider": "fastembed",
            "config": {
                "model": "BAAI/bge-small-en-v1.5",
            }
        },
        "vector_store": {
            "provider": "chroma",
            "config": {
                "collection_name": "omniorb_memory",
                "path": os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory_store"),
            }
        }
    }

    try:
        memory = Memory.from_config(mem_config)
        raw = memory.get_all(user_id="primary_user")
        
        # Debug: show raw structure
        print(f"DEBUG type: {type(raw)}")
        if isinstance(raw, dict):
            print(f"DEBUG keys: {raw.keys()}")
            memories = raw.get("results", raw.get("memories", []))
        elif isinstance(raw, list):
            memories = raw
        else:
            memories = []
            print(f"DEBUG unexpected: {raw}")

        if not memories:
            print("No memories found.")
            return

        print(f"{'='*80}")
        print(f" OmniOrb Long-Term Memories ({len(memories)} total)")
        print(f"{'='*80}\n")

        for i, mem in enumerate(memories, 1):
            if isinstance(mem, dict):
                mem_id = mem.get("id", "?")
                text = mem.get("memory", mem.get("text", "???"))
                created = mem.get("created_at", "")
                updated = mem.get("updated_at", "")

                print(f"  [{i}] ID: {mem_id}")
                print(f"      Memory: {text}")
                if created:
                    print(f"      Created: {created}")
                if updated and updated != created:
                    print(f"      Updated: {updated}")
            else:
                print(f"  [{i}] {mem}")
            print()


        print(f"{'='*80}")
        print(f"To delete a memory: python delete_memory.py <ID>")

    except Exception as e:
        print(f"Error listing memories: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    list_memories()
