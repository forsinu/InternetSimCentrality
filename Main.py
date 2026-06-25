import os
from Topology import Topology
from Scheduler import Scheduler
from AS import AS
from Interpreter import Interpreter
from Engine import Engine

import configparser
import time
import random

from scripts import EnvironmentHandler, HelperProxy


def generate_random_prefix():
    subnet = random.randint(1, 32)

    ip_int = random.getrandbits(32)

    mask = (0xFFFFFFFF << (32 - subnet)) & 0xFFFFFFFF
    network_int = ip_int & mask

    octets = [
        (network_int >> 24) & 0xFF,
        (network_int >> 16) & 0xFF,
        (network_int >> 8) & 0xFF,
        network_int & 0xFF,
    ]

    return ".".join(map(str, octets)) + "/" + str(subnet)


if __name__ == "__main__":
    # config = configparser.ConfigParser()
    # config.read("config.ini")

    env = EnvironmentHandler()
    env.setupSimulatorPaths()

    topology = Topology()
    interpreter = Interpreter(env=env)
    interpreter.loadRoutingInformation(topology)

    util = HelperProxy()
    args = util.parserArgs()

    if args.new_db:
        util.createNewDB(env=env)

    elif not env.getDbDirName():
        util.selectAvailableDB(env=env)

    if args.gen_prefixes:
        prefixes = util.generateNotOverlappingPrefixes(
            numb=args.n_prefixes,
            asn=list(topology.ASes.keys()),
            seed=args.seed,
        )

        util.generateDescriptionFile(env=env, prefixToAsn=prefixes)

    mongoPid = util.runMongoDB(env=env)
    print(f"MongoDB PID: {mongoPid}")

    engine = Engine(env=env)
    if not args.new_db and env.getDbDirName():
        util.resumeFromOldDB(env=env, engine=engine)

    util.createNewExperiment(env)

    engine.run(topology)

    util.decouplePrefixFile(env)

    util.extractBestRoutes(
        topology=topology,
        engine=engine,
        env=env,
    )
