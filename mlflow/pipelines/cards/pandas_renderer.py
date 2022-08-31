"""
Renders the statistics of logged data in a HTML format.
"""
import base64
import numpy as np
import pandas as pd
import sys

from typing import Union, Iterable, Tuple
from facets_overview import feature_statistics_pb2
from mlflow.pipelines.cards import histogram_generator


def DtypeToType(dtype):
    """Converts a Numpy dtype to the FeatureNameStatistics.Type proto enum."""
    fs_proto = feature_statistics_pb2.FeatureNameStatistics
    if dtype.char in np.typecodes["AllFloat"]:
        return fs_proto.FLOAT
    elif (
        dtype.char in np.typecodes["AllInteger"]
        or dtype == bool
        or np.issubdtype(dtype, np.datetime64)
        or np.issubdtype(dtype, np.timedelta64)
    ):
        return fs_proto.INT
    else:
        return fs_proto.STRING


def DtypeToNumberConverter(dtype):
    """Converts a Numpy dtype to a converter method if applicable.
      The converter method takes in a numpy array of objects of the provided
      dtype
      and returns a numpy array of the numbers backing that object for
      statistical
      analysis. Returns None if no converter is necessary.
    Args:
      dtype: The numpy dtype to make a converter for.
    Returns:
      The converter method or None.
    """
    if np.issubdtype(dtype, np.datetime64):

        def DatetimesToNumbers(dt_list):
            return np.array([pd.Timestamp(dt).value for dt in dt_list])

        return DatetimesToNumbers
    elif np.issubdtype(dtype, np.timedelta64):

        def TimedetlasToNumbers(td_list):
            return np.array([pd.Timedelta(td).value for td in td_list])

        return TimedetlasToNumbers
    else:
        return None


def compute_common_stats(column) -> feature_statistics_pb2.CommonStatistics:
    """
    Computes common statistics for a given column in the DataFrame.

    :param column: A column from a DataFrame.
    :return: A CommonStatistics proto.
    """
    common_stats = feature_statistics_pb2.CommonStatistics()
    common_stats.num_missing = column.isnull().sum()
    common_stats.num_non_missing = len(column) - common_stats.num_missing
    # Default set using: https://src.dev.databricks.com/databricks/universe/-/blob/model-monitoring/python/databricks/model_monitoring/rendering/html_renderer.py?L33-L35&subtree=true
    common_stats.min_num_values = 1
    common_stats.max_num_values = 1
    common_stats.avg_num_values = 1.0

    return common_stats


def convert_to_dataset_feature_statistics(
    df: pd.DataFrame,
) -> feature_statistics_pb2.DatasetFeatureStatistics:
    """
    Converts the data statistics from DataFrame format to DatasetFeatureStatistics proto.

    :param df: The DataFrame for which feature statistics need to be computed.
    :return: A DatasetFeatureStatistics proto.
    """
    fs_proto = feature_statistics_pb2.FeatureNameStatistics
    feature_stats = feature_statistics_pb2.DatasetFeatureStatistics()
    pandas_describe = df.describe(datetime_is_numeric=True, include="all")
    feature_stats.num_examples = len(df)
    quantiles_to_get = [x * 10 / 100 for x in range(10 + 1)]
    quantiles = df.quantile(quantiles_to_get)

    for key in df:
        pandas_describe_key = pandas_describe[key]
        current_column_value = df[key]
        feat = feature_stats.features.add(
            type=DtypeToType(current_column_value.dtype), name=key.encode("utf-8")
        )
        if feat.type in (fs_proto.INT, fs_proto.FLOAT):
            feat_stats = feat.num_stats

            converter = DtypeToNumberConverter(current_column_value.dtype)
            if converter:
                date_time_converted = converter(current_column_value)
                current_column_value = pd.DataFrame(date_time_converted)[0]
                pandas_describe_key = current_column_value.describe(
                    datetime_is_numeric=True, include="all"
                )
                quantiles[key] = current_column_value.quantile(quantiles_to_get)

            default_value = 0
            feat_stats.std_dev = pandas_describe_key.get("std", default_value)
            feat_stats.mean = pandas_describe_key.get("mean", default_value)
            feat_stats.min = pandas_describe_key.get("min", default_value)
            feat_stats.max = pandas_describe_key.get("max", default_value)
            feat_stats.median = current_column_value.median()
            feat_stats.num_zeros = (current_column_value == 0).sum()
            feat_stats.common_stats.CopyFrom(compute_common_stats(current_column_value))

            if key in quantiles:
                equal_width_hist = histogram_generator.generate_equal_width_histogram(
                    quantiles=quantiles[key].to_numpy(),
                    num_buckets=10,
                    total_freq=feat_stats.common_stats.num_non_missing,
                )
                if equal_width_hist:
                    feat_stats.histograms.append(equal_width_hist)
                equal_height_hist = histogram_generator.generate_equal_height_histogram(
                    quantiles=quantiles[key].to_numpy(), num_buckets=10
                )
                if equal_height_hist:
                    feat_stats.histograms.append(equal_height_hist)
        elif feat.type == fs_proto.STRING:
            feat_stats = feat.string_stats
            strs = []
            compute_unique_str = current_column_value.dropna()
            for item in compute_unique_str:
                strs.append(
                    item
                    if hasattr(item, "__len__")
                    else item.encode("utf-8")
                    if hasattr(item, "encode")
                    else str(item)
                )

            histogram_categorical_levels_count = None
            feat_stats.avg_length = np.mean(np.vectorize(len)(strs))
            vals, counts = np.unique(strs, return_counts=True)
            feat_stats.unique = pandas_describe_key.get("unique", len(vals))
            sorted_vals = sorted(zip(counts, vals), reverse=True)
            sorted_vals = sorted_vals[:histogram_categorical_levels_count]
            for val_index, val in enumerate(sorted_vals):
                try:
                    if sys.version_info.major < 3 or isinstance(val[1], (bytes, bytearray)):
                        printable_val = val[1].decode("UTF-8", "strict")
                    else:
                        printable_val = val[1]
                except (UnicodeDecodeError, UnicodeEncodeError):
                    printable_val = "__BYTES_VALUE__"
                bucket = feat_stats.rank_histogram.buckets.add(
                    low_rank=val_index,
                    high_rank=val_index,
                    sample_count=np.asscalar(val[0]),
                    label=printable_val,
                )
                if val_index < 2:
                    feat_stats.top_values.add(value=bucket.label, frequency=bucket.sample_count)

            feat_stats.common_stats.CopyFrom(compute_common_stats(current_column_value))

    return feature_stats


def convert_to_proto(df: pd.DataFrame) -> feature_statistics_pb2.DatasetFeatureStatisticsList:
    """
    Converts the data from DataFrame format to DatasetFeatureStatisticsList proto.

    :param df: The DataFrame for which feature statistics need to be computed.
    :return: A DatasetFeatureStatisticsList proto.
    """
    feature_stats = convert_to_dataset_feature_statistics(df)
    feature_stats_list = feature_statistics_pb2.DatasetFeatureStatisticsList()
    feature_stats_list.datasets.append(feature_stats)
    return feature_stats_list


def convert_to_comparison_proto(
    dfs: Iterable[Tuple[str, pd.DataFrame]]
) -> feature_statistics_pb2.DatasetFeatureStatisticsList:
    """
    Converts a collection of named stats DataFrames to a single DatasetFeatureStatisticsList proto.
    :param dfs: The named "glimpses" that contain the DataFrame. Each "glimpse"
        DataFrame has the same properties as the input to `convert_to_proto()`.
    :return: A DatasetFeatureStatisticsList proto which contains a translation
        of the glimpses with the given names.
    """
    feature_stats_list = feature_statistics_pb2.DatasetFeatureStatisticsList()
    for (name, df) in dfs:
        proto = convert_to_dataset_feature_statistics(df)
        proto.name = name
        feature_stats_list.datasets.append(proto)
    return feature_stats_list


def construct_facets_html(
    proto: feature_statistics_pb2.DatasetFeatureStatisticsList, compare: bool = False
) -> str:
    """
    Constructs the facets HTML to visualize the serialized FeatureStatisticsList proto.
    :param proto: A DatasetFeatureStatisticsList proto which contains the statistics for a DataFrame
    :param compare: If True, then the returned visualization switches on the comparison
        mode for several stats.
    :return: the HTML for Facets visualization
    """
    # facets_html_bundle = _get_facets_html_bundle()
    protostr = base64.b64encode(proto.SerializeToString()).decode("utf-8")
    html_template = """
        <script src="https://cdnjs.cloudflare.com/ajax/libs/webcomponentsjs/1.3.3/webcomponents-lite.js"></script>
        <link rel="import" href="https://raw.githubusercontent.com/PAIR-code/facets/1.0.0/facets-dist/facets-jupyter.html" >
        <facets-overview id="facets" proto-input="{protostr}" compare-mode="{compare}"></facets-overview>
    """
    html = html_template.format(protostr=protostr, compare=compare)
    return html


def render_html(inputs: Union[pd.DataFrame, Iterable[Tuple[str, pd.DataFrame]]]) -> None:
    """Rendering the data statistics in a HTML format.

    :param inputs: Either a single "glimpse" DataFrame that contains the statistics, or a
        collection of (name, DataFrame) pairs where each pair names a separate "glimpse"
        and they are all visualized in comparison mode.
    :return: None
    """
    from IPython.display import display as ip_display, HTML

    if isinstance(inputs, pd.DataFrame):
        df: pd.DataFrame = inputs
        proto = convert_to_proto(df)
        compare = False
    else:
        proto = convert_to_comparison_proto(inputs)
        compare = True

    html = construct_facets_html(proto, compare=compare)
    ip_display(HTML(data=html))
