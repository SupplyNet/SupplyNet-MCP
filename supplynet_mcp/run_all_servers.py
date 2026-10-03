"""Start all SupplyNet MCP servers on their configured localhost ports."""

import os
import subprocess
import sys
import time

SERVERS = [
    ("News MCP", "supplynet_mcp.news_server", 8001),
    ("Weather MCP", "supplynet_mcp.weather_server", 8002),
    ("Toll MCP", "supplynet_mcp.toll_rag_server", 8003),
    ("OSRM / Route Optimizer MCP", "supplynet_mcp.osrm_server", 8004),
]


def main() -> None:
    processes = []

    for name, module, port in SERVERS:
        print(f"Starting {name} on port {port}...")

        env = os.environ.copy()
        env["MCP_PORT"] = str(port)

        proc = subprocess.Popen(
            [sys.executable, "-m", module],
            cwd=os.getcwd(),
            env=env,
        )

        processes.append(proc)
        time.sleep(1)

    print("\nSupplyNet MCP servers are running:")

    for name, _, port in SERVERS:
        print(f"  {name}: http://localhost:{port}/mcp")

    print("\nPress Ctrl+C to stop all servers.")

    try:
        while True:
            for proc in processes:
                if proc.poll() is not None:
                    raise RuntimeError("One of the MCP server processes stopped.")
            time.sleep(1)

    except KeyboardInterrupt:
        print("\nStopping MCP servers...")

    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()

        for proc in processes:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()