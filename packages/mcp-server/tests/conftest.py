import sys
from pathlib import Path

# make the mcp_server package importable when running from packages/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
