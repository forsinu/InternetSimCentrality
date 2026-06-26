from __future__ import annotations

from collections import Counter
from multiprocessing import Pool, cpu_count
from pathlib import Path
import argparse
import csv
import logging
import os
import random
import sys
import threading
import time
import tracemalloc

import psutil

try:
    from scripts.env import EnvironmentHandler
except ModuleNotFoundError:
    sys.path.append(str(Path(__file__).resolve().parents[1]))
    from scripts.env import EnvironmentHandler


logger = logging.getLogger(__name__)


def _logMemory(parentPid: int, stopSignal: threading.Event, intervalSeconds: int = 120):
    while not stopSignal.is_set():
        try:
            parent = psutil.Process(parentPid)
            parentMem = parent.memory_info().rss / (1024 * 1024)
            children = parent.children(recursive=True)

            logger.info("--- [PERIODIC MEMORY REPORT] ---")
            logger.info("Main Process (PID %d): %.2f MB", parentPid, parentMem)

            if children:
                for idx, child in enumerate(children, 1):
                    try:
                        childMem = child.memory_info().rss / (1024 * 1024)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue

                    logger.info(
                        "Worker Process %d (PID %d): %.2f MB",
                        idx,
                        child.pid,
                        childMem,
                    )
            else:
                logger.info("No active child worker processes.")

            logger.info("---------------------------------")
        except psutil.NoSuchProcess:
            break
        except Exception as exc:
            logger.error("Error inside memory profiling thread: %s", exc)

        stopSignal.wait(timeout=intervalSeconds)


class ProfileContext:
    def __init__(self, description: str):
        self.description = description
        self.startTime = 0.0

    def __enter__(self):
        self.startTime = time.perf_counter()
        tracemalloc.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        elapsedTime = time.perf_counter() - self.startTime
        _, peakMem = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        logger.info(
            "[%s] Finished in %.2f seconds. Peak Python block memory: %.2f MB",
            self.description,
            elapsedTime,
            peakMem / (1024 * 1024),
        )


def _normalizeAsn(value) -> str:
    asn = str(value).strip()
    if asn.upper().startswith("AS"):
        return asn[2:]

    return asn


def _sortAsn(value: str):
    try:
        return (0, int(_normalizeAsn(value)))
    except ValueError:
        return (1, str(value))


def _formatAsn(value: str):
    normalized = _normalizeAsn(value)
    try:
        return int(normalized)
    except ValueError:
        return normalized


def _updateCountersFromRows(asns, asPaths) -> tuple[Counter, Counter, int]:
    transitCounter = Counter()
    presenceCounter = Counter()
    pathCount = 0

    for receiverAsn, asPath in zip(asns, asPaths):
        if receiverAsn is None or asPath is None:
            continue

        pathNodes = [_normalizeAsn(node) for node in str(asPath).strip().split()]
        if not pathNodes:
            continue

        fullPath = [_normalizeAsn(receiverAsn)] + pathNodes
        pathCount += 1

        presenceCounter.update(set(fullPath))
        if len(fullPath) > 2:
            transitCounter.update(set(fullPath[1:-1]))

    return transitCounter, presenceCounter, pathCount


def _updateCountersFromBatch(batch) -> tuple[Counter, Counter, int]:
    rows = batch.to_pydict()
    return _updateCountersFromRows(rows["ASN"], rows["AS_PATH"])


def _updateCountersFromSelectedBatch(
    batch,
    selectedOffsets: list[int],
) -> tuple[Counter, Counter, int]:
    rows = batch.to_pydict()
    return _updateCountersFromRows(
        [rows["ASN"][offset] for offset in selectedOffsets],
        [rows["AS_PATH"][offset] for offset in selectedOffsets],
    )


def processParquetRowGroup(workItem: tuple[str, int, int, tuple[int, ...] | None]):
    import pyarrow.parquet as pq

    filePath, rowGroupIndex, batchSize, selectedRows = workItem
    transitCounter = Counter()
    presenceCounter = Counter()
    pathCount = 0

    with ProfileContext(f"Worker Processing: {Path(filePath).name}#{rowGroupIndex}"):
        try:
            parquetFile = pq.ParquetFile(filePath)
            batches = parquetFile.iter_batches(
                batch_size=batchSize,
                row_groups=[rowGroupIndex],
                columns=["ASN", "AS_PATH"],
            )

            selectedIndex = 0
            rowGroupOffset = 0
            for batch in batches:
                if selectedRows is None:
                    batchTransit, batchPresence, batchPathCount = (
                        _updateCountersFromBatch(batch)
                    )
                else:
                    batchEnd = rowGroupOffset + batch.num_rows
                    selectedOffsets = []
                    while (
                        selectedIndex < len(selectedRows)
                        and selectedRows[selectedIndex] < batchEnd
                    ):
                        selectedOffsets.append(
                            selectedRows[selectedIndex] - rowGroupOffset
                        )
                        selectedIndex += 1

                    if not selectedOffsets:
                        rowGroupOffset = batchEnd
                        continue

                    batchTransit, batchPresence, batchPathCount = (
                        _updateCountersFromSelectedBatch(batch, selectedOffsets)
                    )
                    rowGroupOffset = batchEnd

                transitCounter.update(batchTransit)
                presenceCounter.update(batchPresence)
                pathCount += batchPathCount

                if selectedRows is None:
                    continue

                if selectedIndex >= len(selectedRows):
                    break
        except Exception as exc:
            logger.error(
                "Error processing %s row group %d: %s",
                filePath,
                rowGroupIndex,
                exc,
            )

    return transitCounter, presenceCounter, pathCount


def _getParquetFiles(inputPath: Path) -> list[Path]:
    if inputPath.is_file():
        return [inputPath]

    return sorted(inputPath.rglob("*.parquet"))


def _collectRowGroups(inputPath: Path) -> list[tuple[str, int, int]]:
    import pyarrow.parquet as pq

    rowGroups = []
    for filePath in _getParquetFiles(inputPath):
        try:
            parquetFile = pq.ParquetFile(filePath)
        except Exception as exc:
            logger.error("Unable to open Parquet file %s: %s", filePath, exc)
            continue

        for rowGroupIndex in range(parquetFile.num_row_groups):
            rowCount = parquetFile.metadata.row_group(rowGroupIndex).num_rows
            if rowCount > 0:
                rowGroups.append((str(filePath), rowGroupIndex, rowCount))

    return rowGroups


def _buildWorkItems(
    inputPath: Path,
    batchSize: int,
    sampleRows: int | None = None,
    sampleSeed: int | None = None,
) -> list[tuple[str, int, int, tuple[int, ...] | None]]:
    rowGroups = _collectRowGroups(inputPath)
    if sampleRows is None:
        return [
            (filePath, rowGroupIndex, batchSize, None)
            for filePath, rowGroupIndex, _ in rowGroups
        ]

    if sampleRows <= 0:
        raise ValueError("sampleRows must be greater than 0")

    totalRows = sum(rowCount for _, _, rowCount in rowGroups)
    if totalRows == 0:
        return []

    if sampleRows >= totalRows:
        logger.warning(
            "Requested %d sample rows, but only %d rows exist. Using all rows.",
            sampleRows,
            totalRows,
        )
        return [
            (filePath, rowGroupIndex, batchSize, None)
            for filePath, rowGroupIndex, _ in rowGroups
        ]

    logger.info(
        "Sampling %d random row(s) from %d total Parquet row(s).",
        sampleRows,
        totalRows,
    )

    sampledRows = sorted(random.Random(sampleSeed).sample(range(totalRows), sampleRows))
    workItems = []
    sampledIndex = 0
    rowGroupStart = 0

    for filePath, rowGroupIndex, rowCount in rowGroups:
        rowGroupEnd = rowGroupStart + rowCount
        selectedRows = []

        while (
            sampledIndex < len(sampledRows) and sampledRows[sampledIndex] < rowGroupEnd
        ):
            selectedRows.append(sampledRows[sampledIndex] - rowGroupStart)
            sampledIndex += 1

        if selectedRows:
            workItems.append((filePath, rowGroupIndex, batchSize, tuple(selectedRows)))

        rowGroupStart = rowGroupEnd

    return workItems


def _aggregateCentrality(
    inputPath: Path,
    numWorkers: int,
    batchSize: int = 100_000,
    sampleRows: int | None = None,
    sampleSeed: int | None = None,
) -> tuple[Counter, Counter, int] | None:
    inputPath = inputPath.expanduser()

    if not inputPath.exists():
        raise FileNotFoundError(f"Best-routes Parquet path does not exist: {inputPath}")

    workItems = _buildWorkItems(
        inputPath,
        batchSize=batchSize,
        sampleRows=sampleRows,
        sampleSeed=sampleSeed,
    )
    if not workItems:
        logger.warning("No Parquet row groups found in %s", inputPath)
        return None

    logger.info("Found %d Parquet row group task(s) to analyze.", len(workItems))

    numWorkers = max(1, min(cpu_count(), numWorkers, len(workItems)))
    logger.info("Using %d worker process(es).", numWorkers)

    totalTransit = Counter()
    totalPresence = Counter()
    totalPaths = 0

    if numWorkers == 1:
        results = map(processParquetRowGroup, workItems)
        for index, result in enumerate(results, 1):
            transitCounter, presenceCounter, pathCount = result
            totalTransit.update(transitCounter)
            totalPresence.update(presenceCounter)
            totalPaths += pathCount
            logger.info(
                "Progress: [%d/%d] row groups aggregated.",
                index,
                len(workItems),
            )
    else:
        with Pool(processes=numWorkers) as pool:
            results = pool.imap_unordered(
                processParquetRowGroup,
                workItems,
                chunksize=1,
            )
            for index, result in enumerate(results, 1):
                transitCounter, presenceCounter, pathCount = result
                totalTransit.update(transitCounter)
                totalPresence.update(presenceCounter)
                totalPaths += pathCount

                if index % 5 == 0 or index == len(workItems):
                    logger.info(
                        "Progress: [%d/%d] row groups aggregated.",
                        index,
                        len(workItems),
                    )

    if totalPaths == 0:
        logger.warning(
            "Completed processing, but no valid paths were parsed. CSV will not be written."
        )
        return None

    logger.info("Total routing paths evaluated: %s", f"{totalPaths:,}")
    return totalTransit, totalPresence, totalPaths


def _buildMetricRows(
    totalTransit: Counter,
    totalPresence: Counter,
    totalPaths: int,
) -> list[dict[str, float | int | str]]:
    metricRows = []
    allASNs = set(totalPresence.keys())
    sortedASNs = sorted(
        allASNs,
        key=lambda asn: (-totalTransit[asn], _sortAsn(asn)),
    )

    for asn in sortedASNs:
        transitCount = totalTransit[asn]
        metricRows.append(
            {
                "asn_key": asn,
                "asn": _formatAsn(asn),
                "presence": totalPresence[asn],
                "transit": transitCount,
                "normalized": transitCount / totalPaths,
            }
        )

    return metricRows


def _writeCentralityCsv(
    outputCSVPath: Path,
    metricRows: list[dict[str, float | int | str]],
) -> None:
    logger.info("Writing centrality results to: %s", outputCSVPath)

    outputCSVPath.parent.mkdir(parents=True, exist_ok=True)
    with outputCSVPath.open("w", newline="", encoding="utf-8") as csvFile:
        writer = csv.writer(csvFile)
        writer.writerow(
            [
                "ASN",
                "TOTAL_PATH_PRESENCE",
                "TRANSIT_CENTRALITY_RAW",
                "TRANSIT_CENTRALITY_NORMALIZED",
            ]
        )

        for row in metricRows:
            writer.writerow(
                [
                    row["asn"],
                    row["presence"],
                    row["transit"],
                    f"{row['normalized']:.6f}",
                ]
            )

    logger.info("Centrality CSV generated successfully.")


def computeCentrality(
    inputPath: Path,
    outputCSVPath: Path,
    numWorkers: int,
    batchSize: int = 100_000,
    sampleRows: int | None = None,
    sampleSeed: int | None = None,
) -> list[dict[str, float | int | str]] | None:
    outputCSVPath = outputCSVPath.expanduser()
    counters = _aggregateCentrality(
        inputPath=inputPath,
        numWorkers=numWorkers,
        batchSize=batchSize,
        sampleRows=sampleRows,
        sampleSeed=sampleSeed,
    )
    if counters is None:
        return None

    metricRows = _buildMetricRows(*counters)
    _writeCentralityCsv(outputCSVPath, metricRows)
    return metricRows


def _buildRunOutputPath(outputCSVPath: Path, runIndex: int, runCount: int) -> Path:
    padding = max(3, len(str(runCount)))
    return outputCSVPath.with_name(
        f"{outputCSVPath.stem}_run_{runIndex:0{padding}d}{outputCSVPath.suffix}"
    )


def _buildSampledOutputPath(outputCSVPath: Path, sampleRows: int) -> Path:
    return outputCSVPath.with_name(
        f"{outputCSVPath.stem}_sampled_{sampleRows}_prefixes{outputCSVPath.suffix}"
    )


def _writeAverageCentralityCsv(
    outputCSVPath: Path,
    runs: list[list[dict[str, float | int | str]]],
) -> None:
    runCount = len(runs)
    presenceSum = Counter()
    transitSum = Counter()
    normalizedSum = Counter()
    observedRuns = Counter()

    for metricRows in runs:
        for row in metricRows:
            asn = row["asn_key"]
            presenceSum[asn] += float(row["presence"])
            transitSum[asn] += float(row["transit"])
            normalizedSum[asn] += float(row["normalized"])
            observedRuns[asn] += 1

    sortedASNs = sorted(
        presenceSum.keys(),
        key=lambda asn: (-(transitSum[asn] / runCount), _sortAsn(asn)),
    )

    logger.info("Writing averaged centrality results to: %s", outputCSVPath)
    outputCSVPath.parent.mkdir(parents=True, exist_ok=True)
    with outputCSVPath.open("w", newline="", encoding="utf-8") as csvFile:
        writer = csv.writer(csvFile)
        writer.writerow(
            [
                "ASN",
                "AVG_TOTAL_PATH_PRESENCE",
                "AVG_TRANSIT_CENTRALITY_RAW",
                "AVG_TRANSIT_CENTRALITY_NORMALIZED",
                "RUNS",
                "OBSERVED_RUNS",
            ]
        )

        for asn in sortedASNs:
            writer.writerow(
                [
                    _formatAsn(asn),
                    f"{presenceSum[asn] / runCount:.6f}",
                    f"{transitSum[asn] / runCount:.6f}",
                    f"{normalizedSum[asn] / runCount:.6f}",
                    runCount,
                    observedRuns[asn],
                ]
            )

    logger.info("Average centrality CSV generated successfully.")


def runRepeatedCentrality(
    inputPath: Path,
    outputCSVPath: Path,
    numWorkers: int,
    batchSize: int,
    sampleRows: int,
    sampleRuns: int,
    sampleSeed: int | None = None,
) -> None:
    outputCSVPath = outputCSVPath.expanduser()
    runMetrics = []

    for runIndex in range(1, sampleRuns + 1):
        runSeed = None if sampleSeed is None else sampleSeed + runIndex - 1
        runOutputPath = _buildRunOutputPath(outputCSVPath, runIndex, sampleRuns)

        logger.info(
            "Starting sampled centrality run %d/%d with %d random path(s).",
            runIndex,
            sampleRuns,
            sampleRows,
        )
        metricRows = computeCentrality(
            inputPath=inputPath,
            outputCSVPath=runOutputPath,
            numWorkers=numWorkers,
            batchSize=batchSize,
            sampleRows=sampleRows,
            sampleSeed=runSeed,
        )
        if metricRows is None:
            logger.warning(
                "Skipping run %d because no metrics were produced.", runIndex
            )
            continue

        runMetrics.append(metricRows)

    if not runMetrics:
        logger.warning(
            "No sampled runs produced metrics. Average CSV will not be written."
        )
        return

    _writeAverageCentralityCsv(outputCSVPath, runMetrics)


def _resolveExperimentDir(env: EnvironmentHandler, experiment: str | None) -> Path:
    if experiment:
        experimentPath = Path(experiment).expanduser()
        if experimentPath.exists():
            return experimentPath

        return env.experimentsDir / experiment

    if env.getExperimentDirName():
        return env.getExperimentDirPath()

    bestRoutesRelPath = Path(
        env.getExperimentPaths().get(
            "bestRoutesFilePath",
            "bestRoutes/bestRoutes.parquet",
        )
    )
    candidates = sorted(
        (
            path
            for path in env.experimentsDir.iterdir()
            if path.is_dir() and (path / bestRoutesRelPath).exists()
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    if not candidates:
        raise FileNotFoundError(
            f"No experiment with {bestRoutesRelPath} found in {env.experimentsDir}"
        )

    return candidates[0]


def _resolveDefaultPaths(
    env: EnvironmentHandler,
    experiment: str | None,
    inputPath: str | None,
    outputPath: str | None,
) -> tuple[Path, Path, Path | None]:
    if inputPath:
        resolvedInput = Path(inputPath).expanduser()
        outputBasePath = (
            resolvedInput if resolvedInput.is_dir() else resolvedInput.parent
        )
        resolvedOutput = (
            Path(outputPath).expanduser()
            if outputPath
            else outputBasePath / "centrality.csv"
        )
        return resolvedInput, resolvedOutput, None

    experimentDir = _resolveExperimentDir(env, experiment)
    bestRoutesRelPath = Path(
        env.getExperimentPaths().get(
            "bestRoutesFilePath",
            "bestRoutes/bestRoutes.parquet",
        )
    )
    metricsDir = (
        env.getExperimentDirStructure()
        .get("dir", {})
        .get(
            "metricsDirPath",
            "metrics",
        )
    )

    resolvedInput = experimentDir / bestRoutesRelPath
    resolvedOutput = (
        Path(outputPath).expanduser()
        if outputPath
        else experimentDir / metricsDir / "centrality.csv"
    )

    return resolvedInput, resolvedOutput, experimentDir


def _configureLogging(logPath: Path | None):
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if logPath:
        logPath.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(logPath, mode="w", encoding="utf-8"))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )


def parseArgs(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compute AS centrality metrics from best-routes Parquet output."
    )
    parser.add_argument(
        "--experiment",
        help="Experiment directory name under the configured experiments base, or a path.",
    )
    parser.add_argument(
        "--input",
        help="Best-routes Parquet file or directory. Overrides --experiment.",
    )
    parser.add_argument(
        "--output",
        help="Output CSV path. Defaults to the experiment metrics directory.",
    )
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--batch-size", type=int, default=100_000)
    parser.add_argument(
        "--sample-rows",
        "--sample-paths",
        dest="sample_rows",
        type=int,
        help="Randomly select N paths and compute centrality only on them.",
    )
    parser.add_argument(
        "--sample-runs",
        "--runs",
        dest="sample_runs",
        type=int,
        default=1,
        help="Run sampled centrality N times, saving each run and an average CSV.",
    )
    parser.add_argument(
        "--sample-seed",
        type=int,
        help="Base seed for sampled runs. Omit for non-deterministic sampling.",
    )
    parser.add_argument("--memory-interval", type=int, default=120)
    parser.add_argument(
        "--no-memory-log",
        action="store_true",
        help="Disable periodic process memory logging.",
    )

    args = parser.parse_args(argv)
    if args.workers <= 0:
        parser.error("--workers must be greater than 0")
    if args.batch_size <= 0:
        parser.error("--batch-size must be greater than 0")
    if args.sample_rows is not None and args.sample_rows <= 0:
        parser.error("--sample-rows/--sample-paths must be greater than 0")
    if args.sample_runs <= 0:
        parser.error("--sample-runs/--runs must be greater than 0")
    if args.sample_runs > 1 and args.sample_rows is None:
        parser.error("--sample-runs/--runs requires --sample-rows/--sample-paths")
    if args.memory_interval <= 0:
        parser.error("--memory-interval must be greater than 0")

    return args


def main(argv: list[str] | None = None) -> int:
    args = parseArgs(argv)

    env = EnvironmentHandler()
    env.setupSimulatorPaths()

    inputPath, outputCSVPath, experimentDir = _resolveDefaultPaths(
        env=env,
        experiment=args.experiment,
        inputPath=args.input,
        outputPath=args.output,
    )
    if args.sample_rows is not None:
        outputCSVPath = _buildSampledOutputPath(outputCSVPath, args.sample_rows)

    logPath = None
    if experimentDir:
        logsDir = (
            env.getExperimentDirStructure()
            .get("dir", {})
            .get(
                "logsDirPath",
                "logs",
            )
        )
        logPath = experimentDir / logsDir / "centrality.log"
    elif outputCSVPath:
        logPath = outputCSVPath.parent / "centrality.log"

    _configureLogging(logPath)
    logger.info("Centrality input: %s", inputPath)
    logger.info("Centrality output: %s", outputCSVPath)

    stopSignal = threading.Event()
    monitorThread = None
    if not args.no_memory_log:
        monitorThread = threading.Thread(
            target=_logMemory,
            args=(os.getpid(), stopSignal, args.memory_interval),
            daemon=True,
        )
        monitorThread.start()
        logger.info(
            "Started background memory monitor thread (interval: %ds).",
            args.memory_interval,
        )

    try:
        with ProfileContext("TOTAL CENTRALITY PIPELINE"):
            if args.sample_runs > 1:
                runRepeatedCentrality(
                    inputPath=inputPath,
                    outputCSVPath=outputCSVPath,
                    numWorkers=args.workers,
                    batchSize=args.batch_size,
                    sampleRows=args.sample_rows,
                    sampleRuns=args.sample_runs,
                    sampleSeed=args.sample_seed,
                )
            else:
                computeCentrality(
                    inputPath=inputPath,
                    outputCSVPath=outputCSVPath,
                    numWorkers=args.workers,
                    batchSize=args.batch_size,
                    sampleRows=args.sample_rows,
                    sampleSeed=args.sample_seed,
                )
    finally:
        stopSignal.set()
        if monitorThread:
            monitorThread.join(timeout=1)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
