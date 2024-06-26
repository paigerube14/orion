# pylint: disable=cyclic-import
"""
module for all utility functions orion uses
"""
# pylint: disable = import-error

from functools import reduce
import json
import logging
import os
import re
import sys

import yaml
import pandas as pd

from hunter.report import Report, ReportType
from hunter.series import Metric, Series
import pyshorteners

from pkg.logrus import SingletonLogger





def run_hunter_analyze(merged_df, test, output):
    """Start hunter analyze function

    Args:
        merged_df (Dataframe): merged dataframe of all the metrics
        test (dict): test dictionary with the each test information
    """
    merged_df["timestamp"] = pd.to_datetime(merged_df["timestamp"])
    merged_df["timestamp"] = merged_df["timestamp"].astype(int) // 10**9
    metrics = {
        column: Metric(1, 1.0)
        for column in merged_df.columns
        if column not in ["uuid","timestamp","buildUrl"]
    }
    data = {
        column: merged_df[column]
        for column in merged_df.columns
        if column not in ["uuid","timestamp","buildUrl"]
    }
    attributes={column: merged_df[column]
                for column in merged_df.columns if column in ["uuid","buildUrl"]}
    series = Series(
        test_name=test["name"],
        branch=None,
        time=list(merged_df["timestamp"]),
        metrics=metrics,
        data=data,
        attributes=attributes,
    )
    change_points = series.analyze().change_points_by_time
    report = Report(series, change_points)
    if output == "text":
        output_table = report.produce_report(
            test_name=test["name"], report_type=ReportType.LOG
        )
        return test["name"], output_table

    if output == "json":
        change_points_by_metric = series.analyze().change_points
        output_json = parse_json_output(merged_df, change_points_by_metric)
        return test["name"], output_json
    return None


def parse_json_output(merged_df, change_points_by_metric):
    """json output generator function

    Args:
        merged_df (pd.Dataframe): the dataframe to be converted to json
        change_points_by_metric (_type_): different change point

    Returns:
        _type_: _description_
    """
    df_json = merged_df.to_json(orient="records")
    df_json = json.loads(df_json)

    for index, entry in enumerate(df_json):
        entry["metrics"] = {
            key: {"value": entry.pop(key), "percentage_change": 0}
            for key in entry.keys() - {"uuid", "timestamp", "buildUrl"}
        }
        entry["is_changepoint"] = False

    for key in change_points_by_metric.keys():
        for change_point in change_points_by_metric[key]:
            index = change_point.index
            percentage_change = (
                (change_point.stats.mean_2 - change_point.stats.mean_1)
                / change_point.stats.mean_1
            ) * 100
            df_json[index]["metrics"][key]["percentage_change"] = percentage_change
            df_json[index]["is_changepoint"] = True

    return df_json


# pylint: disable=too-many-locals
def get_metric_data(ids, index, metrics, match):
    """Gets details metrics basked on metric yaml list

    Args:
        ids (list): list of all uuids
        index (dict): index in es of where to find data
        metrics (dict): metrics to gather data on
        match (Matcher): current matcher instance
        logger (logger): log data to one output

    Returns:
        dataframe_list: dataframe of the all metrics
    """
    logger_instance= SingletonLogger(debug=logging.INFO).logger
    dataframe_list = []
    for metric in metrics:
        metric_name = metric["name"]
        logger_instance.info("Collecting %s", metric_name)
        metric_of_interest = metric["metric_of_interest"]

        if "agg" in metric.keys():
            try:
                cpu = match.get_agg_metric_query(ids, index, metric)
                agg_value = metric["agg"]["value"]
                agg_type = metric["agg"]["agg_type"]
                agg_name = agg_value + "_" + agg_type
                cpu_df = match.convert_to_df(cpu, columns=["uuid", "timestamp", agg_name])
                cpu_df= cpu_df.drop_duplicates(subset=['uuid'],keep='first')
                cpu_df = cpu_df.rename(columns={agg_name: metric_name + "_" + agg_type})
                dataframe_list.append(cpu_df)
                logger_instance.debug(cpu_df)

            except Exception as e:  # pylint: disable=broad-exception-caught
                logger_instance.error(
                    "Couldn't get agg metrics %s, exception %s",
                    metric_name,
                    e,
                )
        else:
            try:
                podl = match.getResults("", ids, index, metric)
                podl_df = match.convert_to_df(
                    podl, columns=["uuid", "timestamp", metric_of_interest]
                )
                podl_df = podl_df.rename(
                    columns={metric_of_interest: metric_name+"_"+metric_of_interest})
                podl_df=podl_df.drop_duplicates()
                dataframe_list.append(podl_df)
                logger_instance.debug(podl_df)
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger_instance.error(
                    "Couldn't get metrics %s, exception %s",
                    metric_name,
                    e,
                )
    return dataframe_list


def get_metadata(test):
    """Gets metadata of the run from each test

    Args:
        test (dict): test dictionary

    Returns:
        dict: dictionary of the metadata
    """
    logger_instance= SingletonLogger(debug=logging.INFO).logger
    metadata = test["metadata"]
    metadata["ocpVersion"] = str(metadata["ocpVersion"])
    logger_instance.debug("metadata" + str(metadata))
    return metadata


def load_config(config):
    """Loads config file

    Args:
        config (str): path to config file
        logger (Logger): logger

    Returns:
        dict: dictionary of the config file
    """
    logger_instance= SingletonLogger(debug=logging.INFO).logger
    try:
        with open(config, "r", encoding="utf-8") as file:
            data = yaml.safe_load(file)
            logger_instance.debug("The %s file has successfully loaded", config)
    except FileNotFoundError as e:
        logger_instance.error("Config file not found: %s", e)
        sys.exit(1)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger_instance.error("An error occurred: %s", e)
        sys.exit(1)
    return data


def get_es_url(data):
    """Gets es url from config or env

    Args:
        data (_type_): config file data
        logger (_type_): logger

    Returns:
        str: es url
    """
    logger_instance= SingletonLogger(debug=logging.INFO).logger
    if "ES_SERVER" in data.keys():
        return data["ES_SERVER"]
    if "ES_SERVER" in os.environ:
        return os.environ.get("ES_SERVER")
    logger_instance.error("ES_SERVER environment variable/config variable not set")
    sys.exit(1)


def get_ids_from_index(metadata, fingerprint_index, uuids, match, baseline):
    """returns the index to be used and runs as uuids

    Args:
        metadata (_type_): metadata from config
        uuids (_type_): uuids collected
        match (_type_): Matcher object

    Returns:
        _type_: index and uuids
    """
    if metadata["benchmark.keyword"] in ["ingress-perf","k8s-netperf"] :
        return uuids
    if baseline == "":
        runs = match.match_kube_burner(uuids,fingerprint_index)
        ids = match.filter_runs(runs, runs)
    else:
        ids = uuids
    return ids

def get_build_urls(index, uuids,match):
    """Gets metadata of the run from each test 
        to get the build url

    Args:
        uuids (list): str list of uuid to find build urls of
        match: the fmatch instance
        

    Returns:
        dict: dictionary of the metadata
    """

    test = match.getResults("",uuids,index,{})
    buildUrls = {run["uuid"]: run["buildUrl"] for run in test}
    return buildUrls


def process_test(test, match, output, uuid, baseline):
    """generate the dataframe for the test given

    Args:
        test (_type_): test from process test
        match (_type_): matcher object
        logger (_type_): logger object
        output (_type_): output file name

    Returns:
        _type_: merged dataframe
    """
    logger_instance= SingletonLogger(debug=logging.INFO).logger
    benchmarkIndex=test['benchmarkIndex']
    fingerprint_index=test['index']
    if uuid in ('', None):
        metadata = get_metadata(test)
    else:
        metadata = filter_metadata(uuid,match)
    logger_instance.info("The test %s has started", test["name"])
    runs = match.get_uuid_by_metadata(metadata)
    uuids = [run["uuid"] for run in runs]
    buildUrls = {run["uuid"]: run["buildUrl"] for run in runs}
    if baseline in ('', None):
        runs = match.get_uuid_by_metadata(metadata)
        uuids = [run["uuid"] for run in runs]
        buildUrls = {run["uuid"]: run["buildUrl"] for run in runs}
        if len(uuids) == 0:
            logger_instance.error("No UUID present for given metadata")
            return None
    else:
        uuids = [uuid for uuid in re.split(' |,',baseline) if uuid]
        uuids.append(uuid)
        buildUrls = get_build_urls(fingerprint_index, uuids,match)
    fingerprint_index=benchmarkIndex
    ids = get_ids_from_index(metadata, fingerprint_index, uuids, match, baseline)

    metrics = test["metrics"]
    dataframe_list = get_metric_data(ids, fingerprint_index, metrics, match)

    for i, df in enumerate(dataframe_list):
        if i != 0 and ('timestamp' in df.columns):
            dataframe_list[i] = df.drop(columns=['timestamp'])

    merged_df = reduce(
        lambda left, right: pd.merge(left, right, on="uuid", how="inner"),
        dataframe_list,
    )
    shortener = pyshorteners.Shortener(timeout=10)
    merged_df["buildUrl"] = merged_df["uuid"].apply(
            lambda uuid: shortener.tinyurl.short(buildUrls[uuid]) #pylint: disable = cell-var-from-loop
        )
    output_file_path = output.split(".")[0] + "-" + test["name"] + ".csv"
    match.save_results(merged_df, csv_file_path=output_file_path)
    return merged_df

def filter_metadata(uuid,match):
    """Gets metadata of the run from each test

    Args:
        uuid (str): str of uuid ot find metadata of
        match: the fmatch instance
        

    Returns:
        dict: dictionary of the metadata
    """
    logger_instance= SingletonLogger(debug=logging.INFO).logger
    test = match.get_metadata_by_uuid(uuid)
    metadata = {
        'platform': '', 
        'clusterType': '', 
        'masterNodesCount': 0,
        'workerNodesCount': 0,
        'infraNodesCount': 0,
        'masterNodesType': '',
        'workerNodesType': '',
        'infraNodesType': '',
        'totalNodesCount': 0,
        'ocpVersion': '',
        'networkType': '',
        'ipsec': '',
        'fips': '',
        'encrypted': '',
        'publish': '',
        'computeArch': '', 
        'controlPlaneArch': ''
    }
    for k,v in test.items():
        if k not in metadata:
            continue
        metadata[k] = v
    metadata['benchmark.keyword'] = test['benchmark']
    metadata["ocpVersion"] = str(metadata["ocpVersion"])

    #Remove any keys that have blank values
    no_blank_meta = {k: v for k, v in metadata.items() if v}
    logger_instance.debug('No blank metadata dict: ' + str(no_blank_meta))
    return no_blank_meta
