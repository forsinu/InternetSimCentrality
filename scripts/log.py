import os
from pathlib import Path
import threading
import time
import psutil as ps
import logging

from scripts.env import EnvironmentHandler


class PerformanceTracker:
    def __init__(self, env: EnvironmentHandler, topology, prefixes: int | None = None):
        self.env = env

        self.loggerRes = self.__setupLogger(
            filePath=env.getExperimentLogPath() / "res.log",
            kind="Res",
        )
        self.loggerMem = self.__setupLogger(
            filePath=env.getExperimentLogPath() / "mem.log",
            kind="Mem",
        )

        self.startTime = 0
        self.endTime = 0
        self.elapsedTime = 0

        sampleInterval = env.experimentConfig["Setting"].get("sampleIntervalSec", 60)
        self.sampleIntervalSec = max(1, int(sampleInterval))

        self.mainProcess = ps.Process(os.getpid())
        self.mongodProcess = self.__getProcess(env.getMongoPid(), "MongoDB")

        self.monitorThread = None
        self.stopSignalMonitor = threading.Event()
        self.memSamples = []

        self.numAS = topology.ASNum
        self.numPrefix = prefixes if prefixes is not None else self.__countPrefixes()
        self.numLink = topology.linkNum

        self.avgMem = 0.0
        self.peakMem = 0.0

        self.threadsUsed = int(env.experimentConfig["Setting"]["threadNum"])
        self.batchSize = int(env.experimentConfig["Setting"]["batchSize"])

        dbDirPath = env.getDbDirPath()
        self.mongodDataPath = dbDirPath / "data" if dbDirPath else None

    def __enter__(self):

        self.loggerRes.info(
            "Starting Simulation. Params:\n"
            f"\t- NUMBER ASes: {self.numAS}\n"
            f"\t- NUMBER PREFIXes: {self.__formatValue(self.numPrefix)}\n"
            f"\t- NUMBER LINKs: {self.numLink}\n"
            f"\t- NUM THREADS: {self.threadsUsed}\n"
            f"\t- BATCH SIZE: {self.batchSize}\n"
        )

        self.startTime = time.perf_counter()

        self.stopSignalMonitor.clear()
        self.memSamples = []

        self.monitorThread = threading.Thread(
            target=self.__sampleMemory,
            daemon=True,
        )
        self.monitorThread.start()

        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stopSignalMonitor.set()
        if self.monitorThread:
            self.monitorThread.join()

        self.endTime = time.perf_counter()
        self.elapsedTime = self.endTime - self.startTime
        countedPrefixes = self.__countPrefixes()
        if countedPrefixes is not None:
            self.numPrefix = countedPrefixes

        finalMem = self.__getMemorySnapshot()
        if finalMem > 0:
            self.memSamples.append(finalMem)

        if exc_type is None:
            status = "COMPLETED"
        elif issubclass(exc_type, KeyboardInterrupt):
            status = "STOPPED (Interrupted by User)"
        else:
            status = f"FAILED ({exc_type.__name__})"
            self.loggerRes.error(
                "Run failed with exception",
                exc_info=(exc_type, exc_value, traceback),
            )

        if self.memSamples:
            self.avgMem = sum(self.memSamples) / len(self.memSamples)
            self.peakMem = max(self.memSamples)
        else:
            self.avgMem = self.peakMem = self.__getMemorySnapshot()

        mongoDataDiskGb = self.__getDirectorySizeGb(self.mongodDataPath)

        self.loggerRes.info(
            f"Run finished:\n"
            f"\t- STATUS: {status}\n"
            f"\t- NUMBER ASes: {self.numAS}\n"
            f"\t- NUMBER PREFIXes: {self.__formatValue(self.numPrefix)}\n"
            f"\t- NUMBER LINKs: {self.numLink}\n"
            f"\t- ELAPSED TIME: {time.strftime('%H:%M:%S', time.gmtime(self.elapsedTime))}\n"
            f"\t- AVG MEM: {self.avgMem:.2f} MB\n"
            f"\t- PEAK MEM: {self.peakMem:.2f} MB\n"
            f"\t- NUM THREADS: {self.threadsUsed}\n"
            f"\t- BATCH SIZE: {self.batchSize}\n"
            f"\t- MONGO DATA DISK: {mongoDataDiskGb:.2f} GB\n"
        )

    def __getMemorySnapshot(self):
        try:
            mainRss = self.__getProcessRss(self.mainProcess)
            mongoRss = self.__getProcessRss(self.mongodProcess)

            childrenRss = 0
            mongoPid = self.mongodProcess.pid if self.mongodProcess else None

            children: list[ps.Process] = self.mainProcess.children(recursive=True)
            for child in children:
                try:
                    if child.pid != mongoPid and child.is_running():
                        childrenRss += child.memory_info().rss
                except (ps.AccessDenied, ps.NoSuchProcess):
                    continue

            totMem = mainRss + mongoRss + childrenRss

            self.loggerMem.info(
                f"Memory Breakdown -> "
                f"Main: {mainRss / (1024**2):.1f}MB | "
                f"Mongo: {mongoRss / (1024**2):.1f}MB | "
                f"Children: {childrenRss / (1024**2):.1f}MB | "
                f"TOTAL: {totMem / (1024**2):.1f}MB"
            )
            return totMem / (1024 * 1024)

        except ps.Error as e:
            self.loggerMem.error(f"Unable to read process memory: {e}")
            return 0.0

    def __sampleMemory(self):
        while not self.stopSignalMonitor.is_set():
            memMb = self.__getMemorySnapshot()
            if memMb > 0:
                self.memSamples.append(memMb)

            self.stopSignalMonitor.wait(timeout=self.sampleIntervalSec)

    def __setupLogger(self, filePath: Path, kind: str):
        logger = logging.getLogger(f"PerformanceTracker{kind}")
        logger.setLevel(logging.INFO)
        logger.propagate = False

        Path(filePath).parent.mkdir(exist_ok=True, parents=True)

        logger.handlers.clear()

        file_handler = logging.FileHandler(str(filePath), mode="a", encoding="utf-8")
        formatter = logging.Formatter("[%(levelname)s][%(asctime)s] %(message)s")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        return logger

    def __getProcess(self, pid: int | None, label: str):
        if pid is None:
            return None

        try:
            return ps.Process(pid)
        except ps.Error as exc:
            self.loggerMem.warning(f"{label} process unavailable: {exc}")
            return None

    def __getProcessRss(self, process) -> int:
        if process is None:
            return 0

        try:
            return process.memory_info().rss
        except (ps.AccessDenied, ps.NoSuchProcess):
            return 0

    def __countPrefixes(self) -> int | None:
        prefixPath = self.env.getPrefixesFilePath()
        if prefixPath and prefixPath.exists():
            with prefixPath.open("r", encoding="utf-8") as prefixFile:
                return sum(1 for line in prefixFile if line.strip())

        descriptionPath = self.env.getDescriptionFilePath()
        if descriptionPath and descriptionPath.exists():
            prefixes = set()

            with descriptionPath.open("r", encoding="utf-8") as descriptionFile:
                for line in descriptionFile:
                    line = line.strip()

                    if line.startswith("AS ") and " announce " in line:
                        prefixes.add(line.rsplit(" announce ", 1)[1].rstrip(";"))

            return len(prefixes)

        return None

    def __getDirectorySizeGb(self, path: Path | None) -> float:
        if path is None or not path.exists():
            return 0.0

        totalBytes = 0
        for root, _, files in os.walk(path):
            rootPath = Path(root)
            for fileName in files:
                try:
                    totalBytes += (rootPath / fileName).stat().st_size
                except OSError:
                    continue

        return totalBytes / (1024**3)

    @staticmethod
    def __formatValue(value):
        return value if value is not None else "unknown"
