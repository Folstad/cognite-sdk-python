# -*- coding: utf-8 -*-
import io
import time
from concurrent.futures import ThreadPoolExecutor as Pool
from functools import partial
from typing import Dict, List
from urllib.parse import quote

import pandas as pd

from cognite._auxiliary._protobuf_descriptors import _api_timeseries_data_v2_pb2
from cognite.client import _utils
from cognite.client._api_client import APIClient, CogniteResponse


class DatapointsResponse(CogniteResponse):
    """Datapoints Response Object."""

    def to_json(self):
        """Returns data as a json object"""
        return self.internal_representation["data"]["items"][0]

    def to_pandas(self):
        """Returns data as a pandas dataframe"""
        return pd.DataFrame(self.internal_representation["data"]["items"][0]["datapoints"])


class DatapointsQuery:
    """Data Query Object for Datapoints.

    Args:
        name (str):           Unique name of the time series.
        aggregates (list):          The aggregate functions to be returned. Use default if null. An empty string must
                                    be sent to get raw data if the default is a set of aggregate functions.
        granularity (str):          The granularity size and granularity of the aggregates.
        start (str, int, datetime): Get datapoints after this time. Format is N[timeunit]-ago where timeunit is w,d,h,m,s.
                                    Example: '2d-ago' will get everything that is up to 2 days old. Can also send time in
                                    ms since epoch or as a datetime object.
        end (str, int, datetime):   Get datapoints up to this time. The format is the same as for start.
    """

    def __init__(self, name, aggregates=None, granularity=None, start=None, end=None, limit=None):
        self.name = name
        self.aggregates = ",".join(aggregates) if aggregates is not None else None
        self.granularity = granularity
        self.start, self.end = _utils.interval_to_ms(start, end)
        if not start:
            self.start = None
        if not end:
            self.end = None
        self.limit = limit


class DatapointsResponseIterator:
    """Iterator for Datapoints Response Objects."""

    def __init__(self, datapoints_objects):
        self.datapoints_objects = datapoints_objects
        self.counter = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self.counter > len(self.datapoints_objects) - 1:
            raise StopIteration
        else:
            self.counter += 1
            return self.datapoints_objects[self.counter - 1]


class DatapointDepth:
    """Data transfer object for Depth datapoints.

       Args:
           depth (double): The depth (in m) of the datapoint
           value (string):     The data value, Can be string or numeric depending on the metric.
       """

    def __init__(self, depth, value):
        self.depth = depth
        self.value = value


class Datapoint:
    """Data transfer object for datapoints.

    Args:
        timestamp (int, datetime): The data timestamp in milliseconds since the epoch (Jan 1, 1970) or as a datetime object.
        value (string):     The data value, Can be string or numeric depending on the metric.
    """

    def __init__(self, timestamp, value):
        self.timestamp = timestamp if isinstance(timestamp, int) else _utils.datetime_to_ms(timestamp)
        self.value = value


class TimeseriesWithDatapoints:
    """Data transfer object for a timeseries with datapoints.

    Args:
        name (str):       Unique ID of time series.
        datapoints (List[stable.datapoints.Datapoint]): List of datapoints in the timeseries.
    """

    def __init__(self, name, datapoints):
        self.name = name
        self.datapoints = datapoints


class LatestDatapointResponse(CogniteResponse):
    """Latest Datapoint Response Object."""

    def to_json(self):
        """Returns data as a json object"""
        return self.internal_representation["data"]["items"][0]

    def to_pandas(self):
        """Returns data as a pandas dataframe"""
        return pd.DataFrame([self.internal_representation["data"]["items"][0]])

    def to_ndarray(self):
        """Returns data as a numpy array"""
        return self.to_pandas().values[0]


class DatapointsClient(APIClient):
    def __init__(self, **kwargs):
        super().__init__(version="0.5", **kwargs)

    def get_datapoints(self, name, start, end=None, aggregates=None, granularity=None, **kwargs) -> DatapointsResponse:
        """Returns a DatapointsObject containing a list of datapoints for the given query.

        This method will automate paging for the user and return all data for the given time period.

        Args:
            name (str):             The name of the timeseries to retrieve data for.

            start (Union[str, int, datetime]):    Get datapoints after this time. Format is N[timeunit]-ago where timeunit is w,d,h,m,s.
                                        E.g. '2d-ago' will get everything that is up to 2 days old. Can also send time in ms since
                                        epoch or a datetime object which will be converted to ms since epoch UTC.

            end (Union[str, int, datetime]):      Get datapoints up to this time. Same format as for start.

            aggregates (list):      The list of aggregate functions you wish to apply to the data. Valid aggregate functions
                                    are: 'average/avg, max, min, count, sum, interpolation/int, stepinterpolation/step'.

            granularity (str):      The granularity of the aggregate values. Valid entries are : 'day/d, hour/h, minute/m,
                                    second/s', or a multiple of these indicated by a number as a prefix e.g. '12hour'.

        Keyword Arguments:
            workers (int):    Number of download workers to run in parallell. Defaults to 10.

            include_outside_points (bool):      No description

            protobuf (bool):        Download the data using the binary protobuf format. Only applicable when getting raw data.
                                    Defaults to True.

            limit (str):            Max number of datapoints to return. If limit is specified, this method will not automate
                                    paging and will return a maximum of 100,000 dps.

        Returns:
            stable.datapoints.DatapointsResponse: A data object containing the requested data with several getter methods with different
            output formats.

        Examples:
            Getting the last 3 days of raw datapoints for a given time series::

                client = CogniteClient()
                res = client.datapoints.get_datapoints(name="my_ts", start="3d-ago")
                print(res.to_pandas())
        """
        start, end = _utils.interval_to_ms(start, end)

        if kwargs.get("limit"):
            return self._get_datapoints_user_defined_limit(
                name,
                aggregates,
                granularity,
                start,
                end,
                limit=kwargs.get("limit"),
                protobuf=kwargs.get("protobuf"),
                include_outside_points=kwargs.get("include_outside_points", False),
            )

        diff = end - start
        num_of_workers = kwargs.get("workers", self._num_of_workers)
        if kwargs.get("include_outside_points") is True:
            num_of_workers = 1

        granularity_ms = 1
        if granularity:
            granularity_ms = _utils.granularity_to_ms(granularity)

        # Ensure that number of steps is not greater than the number data points that will be returned
        steps = min(num_of_workers, max(1, int(diff / granularity_ms)))
        # Make step size a multiple of the granularity requested in order to ensure evenly spaced results
        step_size = _utils.round_to_nearest(int(diff / steps), base=granularity_ms)
        # Create list of where each of the parallelized intervals will begin
        step_starts = [start + (i * step_size) for i in range(steps)]
        args = [{"start": start, "end": start + step_size} for start in step_starts]

        partial_get_dps = partial(
            self._get_datapoints_helper_wrapper,
            name=name,
            aggregates=aggregates,
            granularity=granularity,
            protobuf=kwargs.get("protobuf", True),
            include_outside_points=kwargs.get("include_outside_points", False),
        )

        with Pool(steps) as p:
            datapoints = p.map(partial_get_dps, args)

        concat_dps = []
        [concat_dps.extend(el) for el in datapoints]

        return DatapointsResponse({"data": {"items": [{"name": name, "datapoints": concat_dps}]}})

    def _get_datapoints_helper_wrapper(self, args, name, aggregates, granularity, protobuf, include_outside_points):
        return self._get_datapoints_helper(
            name,
            aggregates,
            granularity,
            args["start"],
            args["end"],
            protobuf=protobuf,
            include_outside_points=include_outside_points,
        )

    def _get_datapoints_helper(self, name, aggregates=None, granularity=None, start=None, end=None, **kwargs):
        """Returns a list of datapoints for the given query.

        This method will automate paging for the given time period.

        Args:
            name (str):       The name of the timeseries to retrieve data for.

            aggregates (list):      The list of aggregate functions you wish to apply to the data. Valid aggregate functions
                                    are: 'average/avg, max, min, count, sum, interpolation/int, stepinterpolation/step'.

            granularity (str):      The granularity of the aggregate values. Valid entries are : 'day/d, hour/h, minute/m,
                                    second/s', or a multiple of these indicated by a number as a prefix e.g. '12hour'.

            start (Union[str, int, datetime]):    Get datapoints after this time. Format is N[timeunit]-ago where timeunit is w,d,h,m,s.
                                        E.g. '2d-ago' will get everything that is up to 2 days old. Can also send time in ms since
                                        epoch or a datetime object which will be converted to ms since epoch UTC.

            end (Union[str, int, datetime]):      Get datapoints up to this time. Same format as for start.

        Keyword Arguments:
            include_outside_points (bool):  No description.

            protobuf (bool):        Download the data using the binary protobuf format. Only applicable when getting raw data.
                                    Defaults to True.

        Returns:
            list of datapoints: A list containing datapoint dicts.
        """
        url = "/timeseries/data/{}".format(quote(name, safe=""))

        use_protobuf = kwargs.get("protobuf", True) and aggregates is None
        limit = self._LIMIT if aggregates is None else self._LIMIT_AGG

        params = {
            "aggregates": aggregates,
            "granularity": granularity,
            "limit": limit,
            "start": start,
            "end": end,
            "includeOutsidePoints": kwargs.get("include_outside_points", False),
        }

        headers = {"accept": "application/protobuf"} if use_protobuf else {}
        datapoints = []
        while (not datapoints or len(datapoints[-1]) == limit) and params["end"] > params["start"]:
            res = self._get(url, params=params, headers=headers)
            if use_protobuf:
                ts_data = _api_timeseries_data_v2_pb2.TimeseriesData()
                ts_data.ParseFromString(res.content)
                res = [{"timestamp": p.timestamp, "value": p.value} for p in ts_data.numericData.points]
            else:
                res = res.json()["data"]["items"][0]["datapoints"]

            if not res:
                break

            datapoints.append(res)
            latest_timestamp = int(datapoints[-1][-1]["timestamp"])
            params["start"] = latest_timestamp + (_utils.granularity_to_ms(granularity) if granularity else 1)
        dps = []
        [dps.extend(el) for el in datapoints]
        return dps

    def _get_datapoints_user_defined_limit(self, name, aggregates, granularity, start, end, limit, **kwargs):
        """Returns a DatapointsResponse object with the requested data.

        No paging or parallelizing is done.

        Args:
            name (str):       The name of the timeseries to retrieve data for.

            aggregates (list):      The list of aggregate functions you wish to apply to the data. Valid aggregate functions
                                    are: 'average/avg, max, min, count, sum, interpolation/int, stepinterpolation/step'.

            granularity (str):      The granularity of the aggregate values. Valid entries are : 'day/d, hour/h, minute/m,
                                    second/s', or a multiple of these indicated by a number as a prefix e.g. '12hour'.

            start (Union[str, int, datetime]):    Get datapoints after this time. Format is N[timeunit]-ago where timeunit is w,d,h,m,s.
                                        E.g. '2d-ago' will get everything that is up to 2 days old. Can also send time in ms since
                                        epoch or a datetime object which will be converted to ms since epoch UTC.

            end (Union[str, int, datetime]):      Get datapoints up to this time. Same format as for start.

            limit (str):            Max number of datapoints to return. Max is 100,000.

        Keyword Arguments:
            include_outside_points (bool):  No description.

            protobuf (bool):        Download the data using the binary protobuf format. Only applicable when getting raw data.
                                    Defaults to True.
        Returns:
            stable.datapoints.DatapointsResponse: A data object containing the requested data with several getter methods with different
            output formats.
        """
        url = "/timeseries/data/{}".format(quote(name, safe=""))

        use_protobuf = kwargs.get("protobuf", True) and aggregates is None

        params = {
            "aggregates": aggregates,
            "granularity": granularity,
            "limit": limit,
            "start": start,
            "end": end,
            "includeOutsidePoints": kwargs.get("include_outside_points", False),
        }
        headers = {"accept": "application/protobuf"} if use_protobuf else {}
        res = self._get(url, params=params, headers=headers)
        if use_protobuf:
            ts_data = _api_timeseries_data_v2_pb2.TimeseriesData()
            ts_data.ParseFromString(res.content)
            res = [{"timestamp": p.timestamp, "value": p.value} for p in ts_data.numericData.points]
        else:
            res = res.json()["data"]["items"][0]["datapoints"]

        return DatapointsResponse({"data": {"items": [{"name": name, "datapoints": res}]}})

    def _split_TimeseriesWithDatapoints_if_over_limit(
        self, timeseries_with_datapoints: TimeseriesWithDatapoints, limit: int
    ) -> List[TimeseriesWithDatapoints]:
        """Takes a TimeseriesWithDatapoints and splits it into multiple so that each has a max number of datapoints equal
        to the limit given.

        Args:
            timeseries_with_datapoints (stable.datapoints.TimeseriesWithDatapoints): The timeseries with data to potentially split up.

        Returns:
            A list of stable.datapoints.TimeSeriesWithDatapoints where each has a maximum number of datapoints equal to the limit given.
        """
        timeseries_with_datapoints_list = []
        if len(timeseries_with_datapoints.datapoints) > limit:
            i = 0
            while i < len(timeseries_with_datapoints.datapoints):
                timeseries_with_datapoints_list.append(
                    TimeseriesWithDatapoints(
                        name=timeseries_with_datapoints.name,
                        datapoints=timeseries_with_datapoints.datapoints[i : i + limit],
                    )
                )
                i += limit
        else:
            timeseries_with_datapoints_list.append(timeseries_with_datapoints)

        return timeseries_with_datapoints_list

    def post_multi_time_series_datapoints(
        self, timeseries_with_datapoints: List[TimeseriesWithDatapoints], **kwargs
    ) -> None:
        """Insert data into multiple timeseries.

        Args:
            timeseries_with_datapoints (List[stable.datapoints.TimeseriesWithDatapoints]): The timeseries with data to insert.

        Keyword Args:
            use_gzip (bool): Whether or not to gzip the request. Defaults to True.

        Returns:
            None

        Examples:
            Posting some dummy datapoints to multiple time series. This example assumes that the time series have
            already been created::

                from cognite.client.stable.datapoints import TimeseriesWithDatapoints, Datapoint

                start = 1514761200000
                my_dummy_data_1 = [Datapoint(timestamp=ms, value=i) for i, ms in range(start, start+100)]
                ts_with_datapoints_1 = TimeSeriesWithDatapoints(name="ts1", datapoints=my_dummy_data_1)

                start = 1503331800000
                my_dummy_data_2 = [Datapoint(timestamp=ms, value=i) for i, ms in range(start, start+100)]
                ts_with_datapoints_2 = TimeSeriesWithDatapoints(name="ts2", datapoints=my_dummy_data_2)

                my_dummy_data = [ts_with_datapoints_1, ts_with_datapoints_2]

                client = CogniteClient()
                res = client.datapoints.post_multi_time_series_datapoints(my_dummy_data)
        """
        url = "/timeseries/data"

        use_gzip = kwargs.get("use_gzip", True)

        ul_dps_limit = 100000

        # Make sure we only work with TimeseriesWithDatapoints objects that has a max number of datapoints
        timeseries_with_datapoints_limited = []
        for entry in timeseries_with_datapoints:
            timeseries_with_datapoints_limited.extend(
                self._split_TimeseriesWithDatapoints_if_over_limit(entry, ul_dps_limit)
            )

        # Group these TimeseriesWithDatapoints if possible so that we upload as much as possible in each call to the API
        timeseries_to_upload_binned = _utils.first_fit(
            list_items=timeseries_with_datapoints_limited, max_size=ul_dps_limit, get_count=lambda x: len(x.datapoints)
        )

        for bin in timeseries_to_upload_binned:
            body = {
                "items": [
                    {"name": ts_with_data.name, "datapoints": [dp.__dict__ for dp in ts_with_data.datapoints]}
                    for ts_with_data in bin
                ]
            }
            self._post(url, body=body, use_gzip=use_gzip)

    def post_datapoints(self, name, datapoints: List[Datapoint]) -> None:
        """Insert a list of datapoints.

        Args:
            name (str):       Name of timeseries to insert to.

            datapoints (List[stable.datapoints.Datapoint]): List of datapoint data transfer objects to insert.

        Returns:
            None

        Examples:
            Posting some dummy datapoints::

                from cognite.client.stable.datapoints import Datapoint

                client = CogniteClient()

                start = 1514761200000
                my_dummy_data = [Datapoint(timestamp=start+off, value=off) for off in range(100)]
                client.datapoints.post_datapoints(ts_name, my_dummy_data)
        """
        url = "/timeseries/data/{}".format(quote(name, safe=""))

        ul_dps_limit = 100000
        i = 0
        while i < len(datapoints):
            body = {"items": [dp.__dict__ for dp in datapoints[i : i + ul_dps_limit]]}
            self._post(url, body=body)
            i += ul_dps_limit

    def get_latest(self, name, before=None) -> LatestDatapointResponse:
        """Returns a LatestDatapointObject containing the latest datapoint for the given timeseries.

        Args:
            name (str):       The name of the timeseries to retrieve data for.

        Returns:
            stable.datapoints.LatestDatapointsResponse: A data object containing the requested data with several getter methods with different
            output formats.

        Examples:
            Get the latest datapoint from a time series before time x::

                client = CogniteClient()
                x = 1514761200000
                client.datapoints.get_latest(name="my_ts", before=x)

        """
        url = "/timeseries/latest/{}".format(quote(name, safe=""))
        params = {"before": before}
        res = self._get(url, params=params)
        return LatestDatapointResponse(res.json())

    def get_multi_time_series_datapoints(
        self, datapoints_queries, start, end=None, aggregates=None, granularity=None, **kwargs
    ) -> DatapointsResponseIterator:
        """Returns a list of DatapointsObjects each of which contains a list of datapoints for the given timeseries.

        This method will automate paging for the user and return all data for the given time period(s).

        Args:
            datapoints_queries (list[stable.datapoints.DatapointsQuery]): The list of DatapointsQuery objects specifying which
                                                                        timeseries to retrieve data for.

            start (Union[str, int, datetime]):    Get datapoints after this time. Format is N[timeunit]-ago where timeunit is w,d,h,m,s.
                                        E.g. '2d-ago' will get everything that is up to 2 days old. Can also send time in ms since
                                        epoch or a datetime object which will be converted to ms since epoch UTC.

            end (Union[str, int, datetime]):      Get datapoints up to this time. Same format as for start.

            aggregates (list, optional):    The list of aggregate functions you wish to apply to the data. Valid aggregate
                                            functions are: 'average/avg, max, min, count, sum, interpolation/int,
                                            stepinterpolation/step'.

            granularity (str):              The granularity of the aggregate values. Valid entries are : 'day/d, hour/h,
                                            minute/m, second/s', or a multiple of these indicated by a number as a prefix
                                            e.g. '12hour'.

        Keyword Arguments:
            include_outside_points (bool):  No description.

        Returns:
            list(stable.datapoints.DatapointsResponse): A list of data objects containing the requested data with several getter methods
            with different output formats.
        """
        url = "/timeseries/dataquery"
        start, end = _utils.interval_to_ms(start, end)

        num_of_dpqs_with_agg = 0
        num_of_dpqs_raw = 0
        for dpq in datapoints_queries:
            if (dpq.aggregates is None and aggregates is None) or dpq.aggregates == "":
                num_of_dpqs_raw += 1
            else:
                num_of_dpqs_with_agg += 1

        items = []
        for dpq in datapoints_queries:
            if dpq.aggregates is None and aggregates is None:
                dpq.limit = int(self._LIMIT / num_of_dpqs_raw)
            else:
                dpq.limit = int(self._LIMIT_AGG / num_of_dpqs_with_agg)
            items.append(dpq.__dict__)
        body = {
            "items": items,
            "aggregates": ",".join(aggregates) if aggregates is not None else None,
            "granularity": granularity,
            "start": start,
            "includeOutsidePoints": kwargs.get("include_outside_points", False),
            "end": end,
        }
        datapoints_responses = []
        has_incomplete_requests = True
        while has_incomplete_requests:
            res = self._post(url=url, body=body).json()["data"]["items"]
            datapoints_responses.append(res)
            has_incomplete_requests = False
            for i, dpr in enumerate(res):
                dpq = datapoints_queries[i]
                if len(dpr["datapoints"]) == dpq.limit:
                    has_incomplete_requests = True
                    latest_timestamp = dpr["datapoints"][-1]["timestamp"]
                    ts_granularity = granularity if dpq.granularity is None else dpq.granularity
                    next_start = latest_timestamp + (_utils.granularity_to_ms(ts_granularity) if ts_granularity else 1)
                else:
                    next_start = end - 1
                    if datapoints_queries[i].end:
                        next_start = datapoints_queries[i].end - 1
                datapoints_queries[i].start = next_start

        results = [{"data": {"items": [{"name": dpq.name, "datapoints": []}]}} for dpq in datapoints_queries]
        for res in datapoints_responses:
            for i, ts in enumerate(res):
                results[i]["data"]["items"][0]["datapoints"].extend(ts["datapoints"])
        return DatapointsResponseIterator([DatapointsResponse(result) for result in results])

    def get_datapoints_frame(self, time_series, aggregates, granularity, start, end=None, **kwargs) -> pd.DataFrame:
        """Returns a pandas dataframe of datapoints for the given timeseries all on the same timestamps.

        This method will automate paging for the user and return all data for the given time period.

        Args:
            time_series (list):  The list of timeseries names to retrieve data for. Each timeseries can be either a string
                                containing the timeseries or a dictionary containing the names of thetimeseries and a
                                list of specific aggregate functions.

            aggregates (list):  The list of aggregate functions you wish to apply to the data for which you have not
                                specified an aggregate function. Valid aggregate functions are: 'average/avg, max, min,
                                count, sum, interpolation/int, stepinterpolation/step'.

            granularity (str):  The granularity of the aggregate values. Valid entries are : 'day/d, hour/h, minute/m,
                                second/s', or a multiple of these indicated by a number as a prefix e.g. '12hour'.

            start (Union[str, int, datetime]):    Get datapoints after this time. Format is N[timeunit]-ago where timeunit is w,d,h,m,s.
                                        E.g. '2d-ago' will get everything that is up to 2 days old. Can also send time in ms since
                                        epoch or a datetime object which will be converted to ms since epoch UTC.

            end (Union[str, int, datetime]):      Get datapoints up to this time. Same format as for start.

        Keyword Arguments:
            limit (str): Max number of rows to return. If limit is specified, this method will not automate
                            paging and will return a maximum of 100,000 rows.

            workers (int):    Number of download workers to run in parallell. Defaults to 10.

        Returns:
            pandas.DataFrame: A pandas dataframe containing the datapoints for the given timeseries. The datapoints for all the
            timeseries will all be on the same timestamps.

        Examples:
            Get a dataframe of aggregated time series data::

                client = CogniteClient()

                res = client.datapoints.get_datapoints_frame(time_series=["ts1", "ts2"],
                                aggregates=["avg"], granularity="30s", start="1w-ago")

                print(res)

            The ``timeseries`` parameter can take a list of strings and/or dicts on the following formats.
            This is useful for specifying aggregate functions on a per time series level::

                Using strings:
                    ['<timeseries1>', '<timeseries2>']

                Using dicts:
                    [{'name': '<timeseries1>', 'aggregates': ['<aggfunc1>', '<aggfunc2>']},
                    {'name': '<timeseries2>', 'aggregates': []}]

                Using both:
                    ['<timeseries1>', {'name': '<timeseries2>', 'aggregates': ['<aggfunc1>', '<aggfunc2>']}]
        """
        if not isinstance(time_series, list):
            raise ValueError("time_series should be a list")
        start, end = _utils.interval_to_ms(start, end)

        if kwargs.get("limit"):
            return self._get_datapoints_frame_user_defined_limit(
                time_series, aggregates, granularity, start, end, limit=kwargs.get("limit")
            )

        diff = end - start
        num_of_workers = kwargs.get("workers") or self._num_of_workers

        granularity_ms = 1
        if granularity:
            granularity_ms = _utils.granularity_to_ms(granularity)

        # Ensure that number of steps is not greater than the number data points that will be returned
        steps = min(num_of_workers, max(1, int(diff / granularity_ms)))
        # Make step size a multiple of the granularity requested in order to ensure evenly spaced results
        step_size = _utils.round_to_nearest(int(diff / steps), base=granularity_ms)
        # Create list of where each of the parallelized intervals will begin
        step_starts = [start + (i * step_size) for i in range(steps)]
        args = [{"start": start, "end": start + step_size} for start in step_starts]

        partial_get_dpsf = partial(
            self._get_datapoints_frame_helper_wrapper,
            time_series=time_series,
            aggregates=aggregates,
            granularity=granularity,
        )

        if steps == 1:
            return self._get_datapoints_frame_helper(time_series, aggregates, granularity, start, end)

        with Pool(steps) as p:
            dataframes = p.map(partial_get_dpsf, args)

        df = pd.concat(dataframes).drop_duplicates(subset="timestamp").reset_index(drop=True)

        return df

    def _get_datapoints_frame_helper_wrapper(self, args, time_series, aggregates, granularity):
        return self._get_datapoints_frame_helper(time_series, aggregates, granularity, args["start"], args["end"])

    def _get_datapoints_frame_helper(self, time_series, aggregates, granularity, start=None, end=None):
        """Returns a pandas dataframe of datapoints for the given timeseries all on the same timestamps.

        This method will automate paging for the user and return all data for the given time period.

        Args:
            time_series (list):     The list of timeseries names to retrieve data for. Each timeseries can be either a string containing the
                                ts name or a dictionary containing the ts name and a list of specific aggregate functions.

            aggregates (list):  The list of aggregate functions you wish to apply to the data for which you have not
                                specified an aggregate function. Valid aggregate functions are: 'average/avg, max, min,
                                count, sum, interpolation/int, stepinterpolation/step'.

            granularity (str):  The granularity of the aggregate values. Valid entries are : 'day/d, hour/h, minute/m,
                                second/s', or a multiple of these indicated by a number as a prefix e.g. '12hour'.

            start (Union[str, int, datetime]):    Get datapoints after this time. Format is N[timeunit]-ago where timeunit is w,d,h,m,s.
                                        E.g. '2d-ago' will get everything that is up to 2 days old. Can also send time in ms since
                                        epoch or a datetime object which will be converted to ms since epoch UTC.

            end (Union[str, int, datetime]):      Get datapoints up to this time. Same format as for start.

        Returns:
            pandas.DataFrame: A pandas dataframe containing the datapoints for the given timeseries. The datapoints for all the
            timeseries will all be on the same timestamps.

        Note:
            The ``timeseries`` parameter can take a list of strings and/or dicts on the following formats::

                Using strings:
                    ['<timeseries1>', '<timeseries2>']

                Using dicts:
                    [{'name': '<timeseries1>', 'aggregates': ['<aggfunc1>', '<aggfunc2>']},
                    {'name': '<timeseries2>', 'aggregates': []}]

                Using both:
                    ['<timeseries1>', {'name': '<timeseries2>', 'aggregates': ['<aggfunc1>', '<aggfunc2>']}]
        """
        url = "/timeseries/dataframe"
        num_aggregates = 0
        for ts in time_series:
            if isinstance(ts, str) or ts.get("aggregates") is None:
                num_aggregates += len(aggregates)
            else:
                num_aggregates += len(ts["aggregates"])

        per_tag_limit = int(self._LIMIT / num_aggregates)

        body = {
            "items": [
                {"name": "{}".format(ts)}
                if isinstance(ts, str)
                else {"name": "{}".format(ts["name"]), "aggregates": ts.get("aggregates", [])}
                for ts in time_series
            ],
            "aggregates": aggregates,
            "granularity": granularity,
            "start": start,
            "end": end,
            "limit": per_tag_limit,
        }
        headers = {"accept": "text/csv"}
        dataframes = []
        while (not dataframes or dataframes[-1].shape[0] == per_tag_limit) and body["end"] > body["start"]:
            res = self._post(url=url, body=body, headers=headers)
            dataframes.append(
                pd.read_csv(io.StringIO(res.content.decode(res.encoding if res.encoding else res.apparent_encoding)))
            )
            if dataframes[-1].empty:
                break
            latest_timestamp = int(dataframes[-1].iloc[-1, 0])
            body["start"] = latest_timestamp + _utils.granularity_to_ms(granularity)
        return pd.concat(dataframes).reset_index(drop=True)

    def _get_datapoints_frame_user_defined_limit(self, time_series, aggregates, granularity, start, end, limit):
        """Returns a DatapointsResponse object with the requested data.

        No paging or parallelizing is done.

        Args:
            time_series (List[str]):       The list of timeseries names to retrieve data for. Each timeseries can be either a string containing the
                                ts name or a dictionary containing the ts name and a list of specific aggregate functions.

            aggregates (list):      The list of aggregate functions you wish to apply to the data. Valid aggregate functions
                                    are: 'average/avg, max, min, count, sum, interpolation/int, stepinterpolation/step'.

            granularity (str):      The granularity of the aggregate values. Valid entries are : 'day/d, hour/h, minute/m,
                                    second/s', or a multiple of these indicated by a number as a prefix e.g. '12hour'.

            start (Union[str, int, datetime]):    Get datapoints after this time. Format is N[timeunit]-ago where timeunit is w,d,h,m,s.
                                        E.g. '2d-ago' will get everything that is up to 2 days old. Can also send time in ms since
                                        epoch or a datetime object which will be converted to ms since epoch UTC.

            end (Union[str, int, datetime]):      Get datapoints up to this time. Same format as for start.

            limit (int):            Max number of rows to retrieve. Max is 100,000.

        Returns:
            stable.datapoints.DatapointsResponse: A data object containing the requested data with several getter methods with different
            output formats.
        """
        url = "/timeseries/dataframe"
        body = {
            "items": [
                {"name": "{}".format(ts)}
                if isinstance(ts, str)
                else {"name": "{}".format(ts["name"]), "aggregates": ts.get("aggregates", [])}
                for ts in time_series
            ],
            "aggregates": aggregates,
            "granularity": granularity,
            "start": start,
            "end": end,
            "limit": limit,
        }

        headers = {"accept": "text/csv"}
        res = self._post(url=url, body=body, headers=headers)
        df = pd.read_csv(io.StringIO(res.content.decode(res.encoding if res.encoding else res.apparent_encoding)))

        return df

    def post_datapoints_frame(self, dataframe) -> None:
        """Write a dataframe

        Args:
            dataframe (DataFrame):  Pandas DataFrame Object containing the timeseries

        Returns:
            None
        """

        try:
            timestamp = dataframe.timestamp
            names = dataframe.drop(["timestamp"], axis=1).columns
        except:
            raise ValueError("DataFrame not on a correct format")

        for name in names:
            data_points = [Datapoint(int(timestamp[i]), dataframe[name].iloc[i]) for i in range(0, len(dataframe))]
            self.post_datapoints(name, data_points)

    def live_data_generator(self, name, update_frequency=1):
        """Generator function which continously polls latest datapoint of a timeseries and yields new datapoints.

        Args:
            name (str): Name of timeseries to get latest datapoints for.

            update_frequency (float): Frequency to pull for data in seconds.

        Yields:
            dict: Dictionary containing timestamp and value of latest datapoint.
        """
        last_timestamp = self.get_latest(name).to_json()["timestamp"]
        while True:
            latest = self.get_latest(name).to_json()
            if last_timestamp == latest["timestamp"]:
                time.sleep(update_frequency)
            else:
                yield latest
            last_timestamp = latest["timestamp"]
