from __future__ import absolute_import
from cbapi.errors import ApiError, InvalidObjectError
from cbapi.models import NewBaseModel, CreatableModelMixin, MutableBaseModel
import logging
from cbapi.psc.threathunter.query import Query, AsyncProcessQuery, TreeQuery, FeedQuery, ReportQuery, WatchlistQuery
import validators
import time

log = logging.getLogger(__name__)


class UnrefreshableModelMixin(NewBaseModel):
    """Represents a model that can't be refreshed, i.e. for which ``reset()``
    is not a valid operation.
    """
    def refresh(self):
        raise ApiError("refresh() called on an unrefreshable model")


class FeedModel(UnrefreshableModelMixin, CreatableModelMixin, MutableBaseModel):
    """A common base class for models used by the Feed and Watchlist APIs.
    """
    pass


class Process(UnrefreshableModelMixin):
    """Represents a process retrieved by one of the CbTH endpoints.
    """
    default_sort = 'last_update desc'
    primary_key = "process_guid"
    validation_url = "/pscr/query/v1/validate"

    @classmethod
    def _query_implementation(cls, cb):
        # This will emulate a synchronous process query, for now.
        return AsyncProcessQuery(cls, cb)

    def __init__(self, cb,  model_unique_id=None, initial_data=None, force_init=False, full_doc=True):
        super(Process, self).__init__(cb, model_unique_id=model_unique_id, initial_data=initial_data,
                                      force_init=force_init, full_doc=full_doc)

    def events(self, **kwargs):
        """Returns a query for events associated with this process's process GUID.

        :param kwargs: Arguments to filter the event query with.
        :return: Returns a Query object with the appropriate search parameters for events
        :rtype: :py:class:`cbapi.psc.threathunter.query.Query`

        Example::

        >>> [print(event) for event in process.events()]
        >>> [print(event) for event in process.events(event_type="modload")]
        """
        query = self._cb.select(Event).where(process_guid=self.process_guid)

        if kwargs:
            query = query.and_(**kwargs)

        return query

    def tree(self):
        """Returns a :py:class:`Tree` of children (and possibly siblings) associated with this process.

        :return: Returns a :py:class:`Tree` object
        :rtype: :py:class:`Tree`

        Example:

        >>> tree = process.tree()
        """
        data = self._cb.select(Tree).where(process_guid=self.process_guid).all()
        return Tree(self._cb, initial_data=data)

    @property
    def parents(self):
        """Returns a query for parent processes associated with this process.

        :return: Returns a Query object with the appropriate search parameters for parent processes, or None if the process has no recorded parent
        :rtype: :py:class:`cbapi.psc.threathunter.query.AsyncProcessQuery` or None
        """
        if "parent_guid" in self._info:
            return self._cb.select(Process).where(process_guid=self.parent_guid)
        else:
            return []

    @property
    def children(self):
        """Returns a list of child processes for this process.

        :return: Returns a list of process objects
        :rtype: list of :py:class:`Process`
        """
        return self.tree().children

    @property
    def siblings(self):
        # NOTE(ww): This shold be provided by the /tree endpoint eventually,
        # but currently isn't.
        raise ApiError("siblings() unimplemented")

    @property
    def process_md5(self):
        """Returns a string representation of the MD5 hash for this process.

        :return: A string representation of the process's MD5.
        :rtype: str
        """
        # NOTE: We have to check _info instead of poking the attribute directly
        # to avoid the missing attrbute login in NewBaseModel.
        if "process_hash" in self._info:
            return next((hsh for hsh in self.process_hash if len(hsh) == 32), None)
        else:
            return None

    @property
    def process_sha256(self):
        """Returns a string representation of the SHA256 hash for this process.

        :return: A string representation of the process's SHA256.
        :rtype: str
        """
        if "process_hash" in self._info:
            return next((hsh for hsh in self.process_hash if len(hsh) == 64), None)
        else:
            return None

    @property
    def process_pids(self):
        """Returns a list of PIDs associated with this process.

        :return: A list of PIDs
        :rtype: list of ints
        """
        # NOTE(ww): This exists because the API returns the list as "process_pid",
        # which is misleading. We just give a slightly clearer name.
        return self.process_pid


class Event(UnrefreshableModelMixin):
    """Events can be queried for via ``CbThreatHunterAPI.select``
    or though an already selected process with ``Process.events()``.
    """
    urlobject = '/pscr/query/v1/events'
    validation_url = '/pscr/query/v1/events/validate'
    default_sort = 'last_update desc'
    primary_key = "process_guid"

    @classmethod
    def _query_implementation(cls, cb):
        return Query(cls, cb)

    def __init__(self, cb,  model_unique_id=None, initial_data=None, force_init=False, full_doc=True):
        super(Event, self).__init__(cb, model_unique_id=model_unique_id, initial_data=initial_data,
                                    force_init=force_init, full_doc=full_doc)


class Tree(UnrefreshableModelMixin):
    """The preferred interface for interacting with Tree models
    is ``Process.tree()``.
    """
    urlobject = '/pscr/query/v2/tree'
    primary_key = 'process_guid'

    @classmethod
    def _query_implementation(cls, cb):
        return TreeQuery(cls, cb)

    def __init__(self, cb, model_unique_id=None, initial_data=None, force_init=False, full_doc=True):
        super(Tree, self).__init__(cb, model_unique_id=model_unique_id, initial_data=initial_data,
                                   force_init=force_init, full_doc=full_doc)

    @property
    def children(self):
        """Returns all of the children of the process that this tree is centered around.

        :return: A list of :py:class:`Process` instances
        :rtype: list of :py:class:`Process`
        """
        return [Process(self._cb, initial_data=child) for child in self.nodes["children"]]


class Feed(FeedModel):
    """Represents a ThreatHunter feed's metadata.
    """
    urlobject = "/threathunter/feedmgr/v1/feed"
    urlobject_single = "/threathunter/feedmgr/v1/feed/{}"
    primary_key = "id"
    swagger_meta_file = "psc/threathunter/models/feed.yaml"

    @classmethod
    def _query_implementation(cls, cb):
        return FeedQuery(cls, cb)

    def __init__(self, cb, model_unique_id=None, initial_data=None):
        item = {}
        reports = []

        if initial_data:
            # NOTE(ww): Some endpoints give us the full Feed, others give us just the FeedInfo.
            if "feedinfo" in initial_data:
                item = initial_data["feedinfo"]
                reports = initial_data.get("reports", [])
            else:
                item = initial_data
        elif model_unique_id:
            resp = cb.get_object(self.urlobject_single.format(model_unique_id))
            item = resp.get("feedinfo", {})
            reports = resp.get("reports", [])

        feed_id = item.get("id")

        super(Feed, self).__init__(cb, model_unique_id=feed_id, initial_data=item,
                                   force_init=False, full_doc=True)

        self._reports = [Report(cb, initial_data=report, feed_id=feed_id) for report in reports]

    def save(self):
        """Saves this feed on the ThreatHunter server.

        :return: The saved feed
        :rtype: :py:class:`Feed`
        """
        self.validate()

        body = {
            'feedinfo': self._info,
            'reports': [report._info for report in self._reports],
        }

        new_info = self._cb.post_object("/threathunter/feedmgr/v1/feed", body).json()
        self._info.update(new_info)
        return self

    def validate(self):
        """Validates this feed's state.

        :raise InvalidObjectError: if the feed's state is invalid
        """
        super(Feed, self).validate()

        if self.access not in ["public", "private"]:
            raise InvalidObjectError("access should be public or private")

        if not validators.url(self.provider_url):
            raise InvalidObjectError("provider_url should be a valid URL")

        for report in self._reports:
            report.validate()

    def delete(self):
        """Deletes this feed from the ThreatHunter server.

        :raise InvalidObjectError: if `id` is missing
        """
        if not self.id:
            raise InvalidObjectError("missing feed ID")

        self._cb.delete_object("/threathunter/feedmgr/v1/feed/{}".format(self.id))

    def update(self, **kwargs):
        """Update this feed's metadata with the given arguments.

        >>> feed.update(access="private")

        :param kwargs: The fields to update
        :type kwargs: dict(str, str)
        :raise InvalidObjectError: if `id` is missing or :py:meth:`validate` fails
        :raise ApiError: if an invalid field is specified
        """
        if not self.id:
            raise InvalidObjectError("missing feed ID")

        for key, value in kwargs.items():
            if key in self._info:
                self._info[key] = value

        self.validate()

        url = "/threathunter/feedmgr/v1/feed/{}/feedinfo".format(self.id)
        new_info = self._cb.put_object(url, self._info).json()
        self._info.update(new_info)

        return self

    @property
    def reports(self):
        """Returns a list of :py:class:`Report` associated with this feed.

        :return: a list of reports
        :rtype: list(:py:class:`Report`)
        """
        return self._cb.select(Report).where(feed_id=self.id)

    def replace_reports(self, reports):
        """Replace this feed's reports with the given reports.

        :param reports: the reports to replace with
        :type reports: list(:py:class:`Report`)
        :raise InvalidObjectError: if `id` is missing
        """
        if not self.id:
            raise InvalidObjectError("missing feed ID")

        rep_dicts = [report._info for report in reports]
        body = {"reports": rep_dicts}

        self._cb.post_object("/threathunter/feedmgr/v1/{}/report".format(self.id), body)

    def append_reports(self, reports):
        """Append the given reports to this feed's current reports.

        :param reports: the reports to append
        :type reports: list(:py:class:`Report`)
        :raise InvalidObjectError: if `id` is missing
        """
        if not self.id:
            raise InvalidObjectError("missing feed ID")

        rep_dicts = [report._info for report in reports]
        rep_dicts += [report._info for report in self._reports]
        body = {"reports": rep_dicts}

        self._cb.post_object("/threathunter/feedmgr/v1/{}/report".format(self.id), body)


class Report(FeedModel):
    """Represents reports retrieved from a ThreatHunter feed.
    """
    urlobject = "/threathunter/feedmgr/v1/feed/{}/report"
    primary_key = "id"
    swagger_meta_file = "psc/threathunter/models/report.yaml"

    @classmethod
    def _query_implementation(cls, cb):
        return ReportQuery(cls, cb)

    def __init__(self, cb, model_unique_id=None, initial_data=None,
                 feed_id=None, from_watchlist=False):

        super(Report, self).__init__(cb, model_unique_id=initial_data.get("id"),
                                     initial_data=initial_data,
                                     force_init=False, full_doc=True)

        # NOTE(ww): Warn instead of failing since we allow Watchlist reports
        # to be created via create(), but we don't actually know that the user
        # intends to use them with a watchlist until they call save().
        if not feed_id and not from_watchlist:
            log.warning("Report created without feed ID or not from watchlist")

        self._feed_id = feed_id
        self._from_watchlist = from_watchlist

        if self.iocs:
            self._iocs = IOC(cb, initial_data=self.iocs, report_id=self.id)
        if self.iocs_v2:
            self._iocs_v2 = [IOC_V2(cb, initial_data=ioc, report_id=self.id) for ioc in self.iocs_v2]

    def save_watchlist(self):
        """Saves this report *as a watchlist report*.

        .. NOTE::
            This method **cannot** be used to save a feed report. To
            save feed reports, create them with `cb.create` and use
            :py:meth:`Feed.replace`.

        :raise InvalidObjectError: if :py:meth:`validate` fails
        """
        self.validate()

        # NOTE(ww): Once saved, this object corresponds to a watchlist report.
        # As such, we need to tell the model to route calls like update()
        # and delete() to the correct (watchlist) endpoints.
        self._from_watchlist = True

        new_info = self._cb.post_object("/threathunter/watchlistmgr/v1/report", self._info).json()
        self._info.update(new_info)
        return self

    def validate(self):
        """Validates this report's state.

        :raise InvalidObjectError: if the report's state is invalid
        """
        super(Report, self).validate()

        if self.link and not validators.url(self.link):
            raise InvalidObjectError("link should be a valid URL")

        if self.iocs_v2:
            [ioc.validate() for ioc in self._iocs_v2]

    def update(self, **kwargs):
        """Update this report with the given arguments.

        .. NOTE::
            The report's timestamp is always updated, regardless of whether
            passed explicitly.

        >>> report.update(title="My new report title")

        :param kwargs: The fields to update
        :type kwargs: dict(str, str)
        :return: The updated report
        :rtype: :py:class:`Report`
        :raises InvalidObjectError: if `id` is missing, or `feed_id` is missing
            and this report is a feed report, or :py:meth:`validate` fails
        """

        if not self.id:
            raise InvalidObjectError("missing Report ID")

        if self._from_watchlist:
            url = "/threathunter/watchlistmgr/v1/report/{}".format(self.id)
        else:
            if not self._feed_id:
                raise InvalidObjectError("missing Feed ID")
            url = "/threathunter/feedmgr/v1/feed/{}/report/{}".format(self._feed_id, self.id)

        for key, value in kwargs.items():
            if key in self._info:
                self._info[key] = value

        # NOTE(ww): Updating reports on the watchlist API appears to require
        # updated timestamps.
        self.timestamp = int(time.time())
        self.validate()

        new_info = self._cb.put_object(url, self._info).json()
        self._info.update(new_info)
        return self

    def delete(self):
        """Deletes this report from the ThreatHunter server.

        >>> report.delete()

        :raises InvalidObjectError: if `id` is missing, or `feed_id` is missing
            and this report is a feed report
        """
        if not self.id:
            raise InvalidObjectError("missing Report ID")

        if self._from_watchlist:
            url = "/threathunter/watchlistmgr/v1/report/{}".format(self.id)
        else:
            if not self._feed_id:
                raise InvalidObjectError("missing Feed ID")
            url = "/threathunter/feedmgr/v1/feed/{}/report/{}".format(self._feed_id, self.id)

        self._cb.delete_object(url)

    @property
    def ignored(self):
        """Returns the ignore status for this report.

        Only watchlist reports have an ignore status.

        >>> if report.ignored:
        ...     report.unignore()

        :return: whether or not this report is ignored
        :rtype: bool
        :raises InvalidObjectError: if `id` is missing or this report is not from a watchlist
        """
        if not self.id:
            raise InvalidObjectError("missing Report ID")
        if not self._from_watchlist:
            raise InvalidObjectError("ignore status only applies to watchlist reports")

        resp = self._cb.get_object("/threathunter/watchlistmgr/v1/report/{}/ignore".format(self.id))
        return resp["ignored"]

    def ignore(self):
        """Sets the ignore status on this report.

        Only watchlist reports have an ignore status.

        :raises InvalidObjectError: if `id` is missing or this report is not from a watchlist
        """
        if not self.id:
            raise InvalidObjectError("missing Report ID")

        if not self._from_watchlist:
            raise InvalidObjectError("ignoring only applies to watchlist reports")

        self._cb.put_object("/threathunter/watchlistmgr/v1/report/{}/ignore".format(self.id), None)

    def unignore(self):
        """Removes the ignore status on this report.

        Only watchlist reports have an ignore status.

        :raises InvalidObjectError: if `id` is missing or this report is not from a watchlist
        """
        if not self.id:
            raise InvalidObjectError("missing Report ID")

        if not self._from_watchlist:
            raise InvalidObjectError("ignoring only applies to watchlist reports")

        self._cb.delete_object("/threathunter/watchlistmgr/v1/report/{}/ignore".format(self.id))

    @property
    def custom_severity(self):
        """Returns the custom severity for this report.

        :return: The custom severity for this report, if it exists
        :rtype: :py:class:`ReportSeverity`
        :raise InvalidObjectError: if `id` is missing or this report is from a watchlist
        """
        if not self.id:
            raise InvalidObjectError("missing report ID")
        if self._from_watchlist:
            raise InvalidObjectError("watchlist reports don't have custom severities")

        resp = self._cb.get_object("/threathunter/watchlistmgr/v1/severity/report/{}".format(self.id))
        return ReportSeverity(self._cb, initial_data=resp)

    @custom_severity.setter
    def custom_severity(self, sev_level):
        """Sets or removed the custom severity for this report

        :param int sev_level: the new severity, or None to remove the custom severity
        :return: The new custom severity, or None if removed
        :rtype: :py:class:`ReportSeverity` or None
        :raise InvalidObjectError: if `id` is missing or this report is from a watchlist
        """
        if not self.id:
            raise InvalidObjectError("missing report ID")
        if self._from_watchlist:
            raise InvalidObjectError("watchlist reports don't have custom severities")

        url = "/threathunter/watchlistmgr/v1/severity/report/{}".format(self.id)

        if sev_level is None:
            self._cb.delete_object(url)
            return

        args = {
            "report_id": self.id,
            "severity": sev_level,
        }

        resp = self._cb.put_object(url, args).json()
        return ReportSeverity(self._cb, initial_data=resp)

    @property
    def iocs_(self):
        """Returns a list of :py:class:`IOC_V2` associated with this report.

        >>> for ioc in report.iocs_:
        ...     print(ioc.values)

        :return: a list of IOCs
        :rtype: list(:py:class:`IOC_V2`)
        """
        if not self.iocs_v2:
            return []

        # NOTE(ww): This name is underscored because something in the model
        # hierarchy is messing up method resolution -- self.iocs and self.iocs_v2
        # are resolving to the attributes rather than the attribute-ified
        # methods.
        return self._iocs_v2


class IOC(FeedModel):
    """Represents a collection of categorized IOCs.
    """
    swagger_meta_file = "psc/threathunter/models/iocs.yaml"

    def __init__(self, cb, model_unique_id=None, initial_data=None, report_id=None):
        """Creates a new IOC instance.

        :raise ApiError: if `initial_data` is `None`
        """
        if not initial_data:
            raise ApiError("IOC can only be initialized from initial_data")

        super(IOC, self).__init__(cb, model_unique_id=model_unique_id, initial_data=initial_data,
                                  force_init=False, full_doc=True)

        self._report_id = report_id

    def validate(self):
        """Validates this IOC structure's state.

        :raise InvalidObjectError: if the IOC structure's state is invalid
        """
        super(IOC, self).validate()

        for md5 in self.md5:
            if not validators(md5):
                raise InvalidObjectError("invalid MD5 checksum: {}".format(md5))
        for ipv4 in self.ipv4:
            if not validators(ipv4):
                raise InvalidObjectError("invalid IPv4 address: {}".format(ipv4))
        for ipv6 in self.ipv6:
            if not validators(ipv6):
                raise InvalidObjectError("invalid IPv6 address: {}".format(ipv6))
        for dns in self.dns:
            if not validators(dns):
                raise InvalidObjectError("invalid domain: {}".format(dns))
        for query in self.query:
            if not self._cb.validate(query["search_query"]):
                raise InvalidObjectError("invalid search query: {}".format(query["search_query"]))


class IOC_V2(FeedModel):
    """Represents a collection of IOCs of a particular type, plus matching criteria and metadata.
    """
    primary_key = "id"
    swagger_meta_file = "psc/threathunter/models/ioc_v2.yaml"

    def __init__(self, cb, model_unique_id=None, initial_data=None, report_id=None):
        """Creates a new IOC_V2 instance.

        :raise ApiError: if `initial_data` is `None`
        """
        if not initial_data:
            raise ApiError("IOC_V2 can only be initialized from initial_data")

        super(IOC_V2, self).__init__(cb, model_unique_id=initial_data.get(self.primary_key),
                                     initial_data=initial_data, force_init=False,
                                     full_doc=True)

        self._report_id = report_id

    def validate(self):
        """Validates this IOC_V2's state.

        :raise InvalidObjectError: if the IOC_V2's state is invalid
        """
        super(IOC_V2, self).validate()

        if self.link and not validators.url(self.link):
            raise InvalidObjectError("link should be a valid URL")

    @property
    def ignored(self):
        """Returns whether or not this IOC is ignored

        >>> if ioc.ignored:
        ...     ioc.unignore()

        :return: the ignore status
        :rtype: bool
        :raise InvalidObjectError: if this IOC is missing an `id` or is not a watchlist IOC
        """
        if not self.id:
            raise InvalidObjectError("missing IOC ID")
        if not self._report_id:
            raise InvalidObjectError("ignore status only applies to watchlist IOCs")

        url = "/threathunter/watchlistmgr/v1/report/{}/ioc/{}/ignore".format(self._report_id, self.id)
        resp = self._cb.get_object(url)
        return resp["ignored"]

    def ignore(self):
        """Sets the ignore status on this IOC.

        Only watchlist IOCs have an ignore status.

        :raises InvalidObjectError: if `id` is missing or this IOC is not from a watchlist
        """
        if not self.id:
            raise InvalidObjectError("missing Report ID")
        if not self._report_id:
            raise InvalidObjectError("ignoring only applies to watchlist IOCs")

        url = "/threathunter/watchlistmgr/v1/report/{}/ioc/{}/ignore".format(self._report_id, self.id)
        self._cb.put_object(url, None)

    def unignore(self):
        """Removes the ignore status on this IOC.

        Only watchlist IOCs have an ignore status.

        :raises InvalidObjectError: if `id` is missing or this IOC is not from a watchlist
        """
        if not self.id:
            raise InvalidObjectError("missing Report ID")
        if not self._report_id:
            raise InvalidObjectError("ignoring only applies to watchlist IOCs")

        url = "/threathunter/watchlistmgr/v1/report/{}/ioc/{}/ignore".format(self._report_id, self.id)
        self._cb.delete_object(url)


class Watchlist(FeedModel):
    """Represents a ThreatHunter watchlist.
    """
    # NOTE(ww): Not documented.
    urlobject = "/threathunter/watchlistmgr/v2/watchlist"
    urlobject_single = "/threathunter/watchlistmgr/v2/watchlist/{}"
    swagger_meta_file = "psc/threathunter/models/watchlist.yaml"

    @classmethod
    def _query_implementation(cls, cb):
        return WatchlistQuery(cls, cb)

    def __init__(self, cb, model_unique_id=None, initial_data=None):
        item = {}

        if initial_data:
            item = initial_data
        elif model_unique_id:
            item = cb.get_object(self.urlobject_single.format(model_unique_id))

        feed_id = item.get("id")

        super(Watchlist, self).__init__(cb, model_unique_id=feed_id, initial_data=item,
                                        force_init=False, full_doc=True)

    def save(self):
        """Saves this watchlist on the ThreatHunter server.

        :return: The saved watchlist
        :rtype: :py:class:`Watchlist`
        :raise InvalidObjectError: if :py:meth:`validate` fails
        """
        self.validate()

        new_info = self._cb.post_object("/threathunter/watchlistmgr/v2/watchlist", self._info).json()
        self._info.update(new_info)
        return self

    def validate(self):
        """Validates this watchlist's state.

        :raise InvalidObjectError: if the watchlist's state is invalid
        """
        super(Watchlist, self).validate()

    def update(self, **kwargs):
        """Updates this watchlist with the given arguments.

        >>> watchlist.update(name="New Name")

        :param kwargs: The fields to update
        :type kwargs: dict(str, str)
        :raise InvalidObjectError: if `id` is missing or :py:meth:`validate` fails
        :raise ApiError: if `report_ids` is given *and* is empty
        """
        if not self.id:
            raise InvalidObjectError("missing Watchlist ID")

        # NOTE(ww): Special case, according to the docs.
        if "report_ids" in kwargs and not kwargs["report_ids"]:
            raise ApiError("can't update a watchlist to have an empty report list")

        for key, value in kwargs.items():
            if key in self._info:
                self._info[key] = value

        self.validate()

        new_info = self._cb.put_object("/threathunter/watchlistmgr/v2/watchlist/{}".format(self.id), self._info).json()
        self._info.update(new_info)

    @property
    def classifier_(self):
        """Returns the classifier key and value, if any, for this watchlist.

        :rtype: tuple(str, str) or None
        """
        classifier_dict = self._info.get("classifier")

        if not classifier_dict:
            return None

        return (classifier_dict["key"], classifier_dict["value"])

    def delete(self):
        """Deletes this watchlist from the ThreatHunter server.

        :raise InvalidObjectError: if `id` is missing
        """
        if not self.id:
            raise InvalidObjectError("missing Watchlist ID")

        self._cb.delete_object("/threathunter/watchlistmgr/v1/watchlist/{}".format(self.id))

    def enable_alerts(self):
        """Enable alerts for this watchlist. Alerts are not retroactive.

        :raise InvalidObjectError: if `id` is missing
        """
        if not self.id:
            raise InvalidObjectError("missing Watchlist ID")

        self._cb.put_object("/threathunter/watchlistmgr/v1/watchlist/{}/alert".format(self.id), None)

    def disable_alerts(self):
        """Disable alerts for this watchlist.

        :raise InvalidObjectError: if `id` is missing
        """
        if not self.id:
            raise InvalidObjectError("missing Watchlist ID")

        self._cb.delete_object("/threathunter/watchlistmgr/v1/watchlist/{}/alert".format(self.id))

    def enable_tags(self):
        """Enable tagging for this watchlist.

        :raise InvalidObjectError: if `id` is missing
        """
        if not self.id:
            raise InvalidObjectError("missing Watchlist ID")

        self._cb.put_object("/threathunter/watchlistmgr/v1/watchlist/{}/tag".format(self.id), None)

    def disable_tags(self):
        """Disable tagging for this watchlist.

        :raise InvalidObjectError: if `id` is missing
        """
        if not self.id:
            raise InvalidObjectError("missing Watchlist ID")

        self._cb.delete_object("/threathunter/watchlistmgr/v1/watchlist/{}/tag".format(self.id))

    @property
    def feed(self):
        """Returns the feed linked to this watchlist, if there is one.

        :return: the feed linked to this watchlist, if any
        :rtype: :py:class:`Feed` or None
        """
        if not self.classifier:
            return None
        if self.classifier["key"] != "feed_id":
            log.warning("Unexpected classifier type: {}".format(self.classifier["key"]))
            return None

        return self._cb.select(Feed, self.classifier["value"])

    @property
    def reports(self):
        """Returns a list of :py:class:`Report` instances associated with this watchlist.

        .. NOTE::
            If this watchlist is a classifier (i.e. feed-linked) watchlist,
            `reports` will be empty. To get the reports associated with the linked
            feed, use :py:attr:`feed` like:

            >>> for report in watchlist.feed.reports:
            ...     print(report.title)

        :return: A list of reports
        :rtype: list(:py:class:`Report`)
        """
        if not self.report_ids:
            return []

        reports_ = []
        for rep_id in self.report_ids:
            resp = self._cb.get_object("/threathunter/watchlistmgr/v1/report/{}".format(rep_id))
            reports_.append(Report(self._cb, initial_data=resp, from_watchlist=True))

        return reports_


class ReportSeverity(FeedModel):
    """Represents severity information for a watchlist report.
    """
    primary_key = "report_id"
    swagger_meta_file = "psc/threathunter/models/report_severity.yaml"

    def __init__(self, cb, initial_data=None):
        if not initial_data:
            raise ApiError("ReportSeverity can only be initialized from initial_data")

        super(ReportSeverity, self).__init__(cb, model_unique_id=initial_data.get(self.primary_key),
                                             initial_data=initial_data, force_init=False,
                                             full_doc=True)
