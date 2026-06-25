from datetime import datetime, timezone
from typing import Any
from uuid import uuid4
import json
from pathlib import Path


class EnvironmentHandler:
    GLOBAL_CONFIG_PATH = Path("global.config.json")
    EXPERIMENT_CONFIG_PATH = Path("experiment.config.json")
    CURRENT_PATH = Path("./")

    def __init__(self):
        self.globalConfig = json.load(self.GLOBAL_CONFIG_PATH.open(encoding="utf-8"))
        self.experimentConfig = json.load(
            self.EXPERIMENT_CONFIG_PATH.open(encoding="utf-8")
        )

        self.pathConfig = self.experimentConfig["Path"]

        self.dbDirName = self.experimentConfig.get("dbDirName", None)
        self.experimentDirName = self.experimentConfig.get("experimentDirName", None)
        self.mongoPid = None

    def __getitem__(self, key: str) -> Any:
        return self.experimentConfig[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.experimentConfig.get(key, default)

    def setupSimulatorPaths(self):

        self.basesDir = Path(self.globalConfig["baseSimulationPath"])
        self.basesDir.mkdir(parents=True, exist_ok=True)

        self.databaseDir = self.basesDir / self.globalConfig.get(
            "baseDatabasesDirName", "databases"
        )
        self.databaseDir.mkdir(parents=True, exist_ok=True)

        self.experimentsDir = self.basesDir / self.globalConfig.get(
            "baseExperimentsDirName", "experiments"
        )
        self.experimentsDir.mkdir(parents=True, exist_ok=True)

    def getDescriptionFilePath(self) -> Path:
        return self.CURRENT_PATH / self.pathConfig.get("descriptionFilePath")

    def getExperimentDirStructure(self) -> dict[str, dict[str, Any]]:
        return self.globalConfig["experimentDirStructure"]

    def getExperimentPaths(self) -> dict[str, Any]:
        return self.experimentConfig["Path"]

    def getExperimentDirName(self) -> str | None:
        return self.experimentDirName

    def setExperimentDirName(self, name: str):
        self.experimentDirName = name

    def getExperimentDirPath(self) -> Path | None:
        if not self.experimentDirName:
            return None

        return self.experimentsDir / self.experimentDirName

    def getDbDirPath(self) -> Path | None:
        if not self.dbDirName:
            return None

        return self.databaseDir / self.dbDirName

    def getPrefixesFilePath(self) -> Path | None:
        if not self.dbDirName:
            return None

        return self.getDbDirPath() / self.experimentConfig.get(
            "prefixesFilePath",
            "prefixes.txt",
        )

    def getPrefixesFileName(self) -> str:
        return self.experimentConfig.get("prefixesFilePath", "prefixes.txt")

    def getDbDirName(self) -> str | None:
        return self.dbDirName

    def setDbDirName(self, name: str):
        self.dbDirName = name

    def getMongoPid(self) -> int | None:
        return self.mongoPid

    def setMongoPid(self, pid: int | None):
        self.mongoPid = pid
