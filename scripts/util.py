from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from typing import TYPE_CHECKING

import argparse

from scripts.env import EnvironmentHandler

if TYPE_CHECKING:
    from Engine import Engine


class HelperProxy:
    @staticmethod
    def generateNotOverlappingPrefixes(
        numb: int,
        asn: list[int],
        seed: int = 14,
    ) -> dict[int, dict[int, dict[int, int]]]:
        import random

        if numb <= 0 or not asn:
            raise ValueError("aut cannot be empty")

        rng = random.Random(seed)

        prefixes: dict[int, dict[int, dict[int, int]]] = {}
        generated: set[tuple[int, int, int]] = set()

        toGenerate = min(len(asn), numb)

        # A /24 prefix is identified by the first 3 octets.
        # Example: 10.20.30.0/24 -> (10, 20, 30)
        while len(generated) < toGenerate:
            intIP = rng.getrandbits(32)

            firstOctet = (intIP >> 24) & 0xFF
            secondOctet = (intIP >> 16) & 0xFF
            thirdOctet = (intIP >> 8) & 0xFF

            prefixKey = (firstOctet, secondOctet, thirdOctet)

            # Avoid overlapping duplicated /24 prefixes.
            if prefixKey in generated:
                continue

            generated.add(prefixKey)

            # Randomly assign one ASN from the given list.
            asNumb = rng.choice(asn)

            prefixes.setdefault(firstOctet, {}).setdefault(secondOctet, {})[
                thirdOctet
            ] = asNumb

        return prefixes

    @staticmethod
    def generateDescriptionFile(
        env: EnvironmentHandler,
        prefixToAsn: dict[int, dict[int, dict[int, int]]],
        storePrefixes: bool = True,
    ) -> None:
        outputFile = env.getDescriptionFilePath()
        outputFile.parent.mkdir(parents=True, exist_ok=True)

        # prefixesFile = env.getPrefixesFilePath()

        with (
            outputFile.open("w", encoding="utf-8") as outDesc,
            # prefixesFile.open("w", encoding="utf-8") as outPrefixes,
        ):
            for first in sorted(prefixToAsn):
                for second in sorted(prefixToAsn[first]):
                    for third in sorted(prefixToAsn[first][second]):
                        asn = prefixToAsn[first][second][third]
                        prefix = f"{first}.{second}.{third}.0/24"

                        outDesc.write(f"AS {asn} announce {prefix};\n")
                        # outPrefixes.write(f"{prefix}\n")

            outDesc.write("Simulate;\n")

            if storePrefixes:
                outDesc.write("Store;\n")

    @staticmethod
    def getAvailableDB(env: EnvironmentHandler) -> list[Path]:
        prefixFileName = env.getPrefixesFileName()

        return sorted(
            (
                path
                for path in env.databaseDir.iterdir()
                if path.is_dir() and (path / prefixFileName).exists()
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )

    @staticmethod
    def selectAvailableDB(env: EnvironmentHandler) -> Path:
        availableDBs = HelperProxy.getAvailableDB(env)
        if not availableDBs:
            raise ValueError(f"No available database found in {env.databaseDir}")

        selected = HelperProxy._selectDBWithTerminalMenu(availableDBs)
        env.setDbDirName(selected.name)
        return selected

    @staticmethod
    def _selectDBWithTerminalMenu(availableDBs: list[Path]) -> Path:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return HelperProxy._selectDBWithPrompt(availableDBs)

        try:
            import curses
        except ImportError:
            return HelperProxy._selectDBWithPrompt(availableDBs)

        def draw(stdscr):
            curses.curs_set(0)
            index = 0

            while True:
                stdscr.clear()
                stdscr.addstr(0, 0, "Choose a database")
                stdscr.addstr(1, 0, "Use up/down, Enter to select, q to quit")

                height, width = stdscr.getmaxyx()
                menuStartRow = 3
                visibleRows = max(1, height - menuStartRow)
                start = max(
                    0,
                    min(index - visibleRows + 1, len(availableDBs) - visibleRows),
                )

                for offset, dbPath in enumerate(
                    availableDBs[start : start + visibleRows]
                ):
                    row = menuStartRow + offset
                    if row >= height:
                        break

                    itemIndex = start + offset
                    marker = "> " if itemIndex == index else "  "
                    label = f"{marker}{dbPath.name}"
                    stdscr.addstr(row, 0, label[: max(0, width - 1)])

                key = stdscr.getch()
                if key in (curses.KEY_UP, ord("k")):
                    index = (index - 1) % len(availableDBs)
                elif key in (curses.KEY_DOWN, ord("j")):
                    index = (index + 1) % len(availableDBs)
                elif key in (curses.KEY_ENTER, 10, 13):
                    return index
                elif key in (ord("q"), 27):
                    raise KeyboardInterrupt

        selectedIndex = curses.wrapper(draw)
        return availableDBs[selectedIndex]

    @staticmethod
    def _selectDBWithPrompt(availableDBs: list[Path]) -> Path:
        print("Choose a database:")
        for index, dbPath in enumerate(availableDBs, start=1):
            print(f"{index}. {dbPath.name}")

        while True:
            value = input("Database number: ").strip()
            try:
                selectedIndex = int(value) - 1
            except ValueError:
                print("Please enter a valid number.")
                continue

            if 0 <= selectedIndex < len(availableDBs):
                return availableDBs[selectedIndex]

            print("Please choose one of the listed databases.")

    @staticmethod
    def resumeFromOldDB(
        env: EnvironmentHandler,
        engine: "Engine",
    ):
        if not env.getDbDirName():
            raise ValueError("Provide a valid database directory name!")

        dbDirPath = env.getDbDirPath()
        prefixesFilePath = env.getPrefixesFilePath()

        if not (dbDirPath.exists() and prefixesFilePath.exists()):
            raise ValueError("Impossible to find the DB!!")

        with prefixesFilePath.open("r", encoding="utf-8") as inputPrefixes:
            engine.prefixInDB = {line.strip() for line in inputPrefixes if line.strip()}

    @staticmethod
    def runMongoDB(
        env: EnvironmentHandler,
        port: int = 27017,
    ) -> int:

        basePath = env.getDbDirPath()
        dataDir = basePath / "data"
        logsDir = basePath / "logs"
        logPath = logsDir / "mongod.log"
        pidPath = basePath / "mongod.pid"

        dataDir.mkdir(parents=True, exist_ok=True)
        logsDir.mkdir(parents=True, exist_ok=True)

        wiredTigerCacheSizeGB = int(
            env.experimentConfig["Setting"].get("cacheDbSizeGb", 16)
        )

        HelperProxy.killMongoDB(port=port, pidPath=pidPath)

        if shutil.which("mongod") is None:
            raise RuntimeError("mongod executable not found in PATH")

        command = [
            "mongod",
            "--dbpath",
            str(dataDir),
            "--logpath",
            str(logPath),
            "--wiredTigerCacheSizeGB",
            str(wiredTigerCacheSizeGB),
            "--port",
            str(port),
            "--fork",
            "--pidfilepath",
            str(pidPath),
        ]

        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            output = "\n".join(part for part in (result.stdout, result.stderr) if part)
            logTail = HelperProxy._readLogTail(logPath)
            if logTail:
                output = f"{output}\n\nMongoDB log tail:\n{logTail}"

            raise RuntimeError(f"Unable to start MongoDB:\n{output}")

        pid = HelperProxy._readPidFile(pidPath) or HelperProxy._parseForkedMongoPid(
            result.stdout,
            result.stderr,
        )
        if pid is None:
            pids = HelperProxy._findMongoPids(port=port)
            pid = pids[0] if pids else None

        if pid is None:
            raise RuntimeError("MongoDB started but its PID could not be retrieved")

        if not HelperProxy._processExists(pid):
            raise RuntimeError(f"MongoDB started but PID {pid} is not running")

        env.setMongoPid(pid)
        return pid

    @staticmethod
    def killMongoDB(
        port: int = 27017,
        pidPath: Path | None = None,
        timeoutSeconds: float = 10.0,
    ) -> None:
        pids = set()
        if pidPath:
            pid = HelperProxy._readPidFile(pidPath)
            if pid:
                pids.add(pid)

        pids.update(HelperProxy._findMongoPids(port=port))
        pids.discard(os.getpid())

        if not pids:
            if pidPath and pidPath.exists():
                pidPath.unlink()
            return

        for pid in pids:
            HelperProxy._killProcess(pid, signal.SIGTERM)

        deadline = time.monotonic() + timeoutSeconds
        while time.monotonic() < deadline:
            runningPids = {pid for pid in pids if HelperProxy._processExists(pid)}
            if not runningPids:
                break
            pids = runningPids
            time.sleep(0.2)

        for pid in pids:
            if HelperProxy._processExists(pid):
                HelperProxy._killProcess(pid, signal.SIGKILL)

        if pidPath and pidPath.exists():
            pidPath.unlink()

    # @staticmethod
    # def _resolveScratchBase(
    #     env: EnvironmentHandler,
    #     scratchBase: str | Path | None,
    # ) -> Path:
    #     rawScratchBase = (
    #         scratchBase
    #         or os.environ.get("SCRATCH_BASE")
    #         or env.globalConfig.get("baseSimulationPath")
    #     )
    #     if not rawScratchBase:
    #         raise ValueError("SCRATCH_BASE is not set and no fallback path is configured")

    #     return Path(rawScratchBase).expanduser().resolve()

    @staticmethod
    def _readPidFile(pidPath: Path) -> int | None:
        try:
            value = pidPath.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None

        try:
            return int(value)
        except ValueError:
            return None

    @staticmethod
    def _parseForkedMongoPid(*outputs: str) -> int | None:
        text = "\n".join(output for output in outputs if output)
        match = re.search(r"forked process:\s*(\d+)", text)
        if not match:
            return None

        return int(match.group(1))

    @staticmethod
    def _readLogTail(logPath: Path, lineCount: int = 40) -> str:
        try:
            lines = logPath.read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            return ""

        return "\n".join(lines[-lineCount:])

    @staticmethod
    def _findMongoPids(port: int) -> list[int]:
        if shutil.which("pgrep") is None:
            return []

        pattern = rf"mongod.*--port(=| ){port}([^0-9]|$)"
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
        )
        if result.returncode not in (0, 1):
            return []

        pids = []
        for line in result.stdout.splitlines():
            try:
                pids.append(int(line.strip()))
            except ValueError:
                continue

        return pids

    @staticmethod
    def _processExists(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

        return True

    @staticmethod
    def _killProcess(pid: int, sig: signal.Signals) -> None:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return

    @staticmethod
    def createNewDB(env: EnvironmentHandler) -> Path:
        dirName = env.getDbDirName()

        if not dirName:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            uniqueId = uuid4().hex[:8]

            dirName = f"db_{timestamp}_{uniqueId}"

        env.setDbDirName(name=dirName)

        db = env.getDbDirPath()
        if db.exists():
            raise ValueError("Given name for the DB just exists. Cannot overwrite!")

        db.mkdir(parents=True)

        return db

    @staticmethod
    def createNewExperiment(env: EnvironmentHandler) -> Path:
        dirName = env.getExperimentDirName()

        if not dirName:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            uniqueId = uuid4().hex[:8]

            dirName = f"exp_{timestamp}_{uniqueId}"

        env.setExperimentDirName(name=dirName)

        sim = env.getExperimentDirPath()
        sim.mkdir(parents=True)

        structure = env.getExperimentDirStructure()

        dir = structure["dir"]
        metrics = sim / dir.get("metricsDirPath", "metrics")
        logs = sim / dir.get("logsDirPath", "logs")

        metrics.mkdir(parents=True)
        logs.mkdir(parents=True)

        # files = structure["file"]
        shutil.copy(env.EXPERIMENT_CONFIG_PATH, sim)
        shutil.copy(env.getDescriptionFilePath(), sim)

        return sim

    @staticmethod
    def parserArgs() -> argparse.Namespace:
        parser = argparse.ArgumentParser()

        parser.add_argument("--new-db", action="store_true")

        parser.add_argument("--gen-prefixes", action="store_true")

        parser.add_argument("--n-prefixes", type=int)

        parser.add_argument("--seed", type=int, default=14)

        args = parser.parse_args()

        if args.n_prefixes and not args.gen_prefixes:
            raise argparse.ArgumentError(
                message="Use --numb-prefixes with --gen-prefixes"
            )

        return parser.parse_args()

    @staticmethod
    def decouplePrefixFile(env: EnvironmentHandler):
        prefixesFilePath = env.getPrefixesFilePath()

        if shutil.which("sort") is None:
            raise RuntimeError("sort executable not found in PATH")

        command = ["sort", "-u", "-o", str(prefixesFilePath), str(prefixesFilePath)]

        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            raise ChildProcessError(
                f"Not able to decouple prefixes inside {str(prefixesFilePath)}"
            )
