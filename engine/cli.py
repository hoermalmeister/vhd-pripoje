"""Příkazová řádka:  python -m engine.cli "Ostrava-Svinov" 2026-11-02 113 3360
(stanice, datum, přípojný vlak = čím přijíždím, návazný vlak = na co přestupuji)"""
import json, os, sys
from .evaluate import Engine

if __name__ == "__main__":
    data = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))
    e = Engine(data, os.environ.get("TRAINS_DB"))
    print(json.dumps(e.evaluate(sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])), ensure_ascii=False, indent=2))
