import os
import sys
import logging

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

def add_memory(text):
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
        memory.add(text, user_id="primary_user")
        print(f"Successfully added memory: {text}")
    except Exception as e:
        print(f"Error adding memory: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python add_memory.py <text>")
        sys.exit(1)
    add_memory(" ".join(sys.argv[1:]))
