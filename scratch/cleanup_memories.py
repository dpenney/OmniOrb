import chromadb
import os

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory_store")
client = chromadb.PersistentClient(path=DB)
col = client.get_collection("omniorb_memory")

to_delete = [
    "aa8700d2-be80-43ef-a8d4-a618b629d981",  # [Placeholder 1]
    "914c1f1b-b25e-4cdc-adf0-a27f491e808d",  # [Placeholder 2]
    "e5a16242-8f0b-40ee-8046-2dbb73b53e47",  # [Placeholder 3]
]

col.delete(ids=to_delete)
for mid in to_delete:
    print(f"  Deleted: {mid}")
print(f"\nRemaining: {col.count()}")
