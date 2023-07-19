# session.py

"""Provide consumer session to get Bloomberg Service.

This component implements a consumer session for getting services.
"""

from __future__ import annotations
from weakref import ref, ReferenceType  # pylint: disable=unused-import
from typing import Optional, Callable, List
import sys
import traceback
import os
import functools
import atexit
from enum import Enum
from .abstractsession import AbstractSession
from .event import Event
from . import exception
from .exception import _ExceptionUtil
from . import internals
from .internals import CorrelationId
from .sessionoptions import SessionOptions
from .requesttemplate import RequestTemplate
from .utils import get_handle, MetaClassForClassesWithEnums
from . import typehints  # pylint: disable=unused-import
from .typehints import BlpapiEventHandle

# pylint: disable=too-many-arguments,protected-access,bare-except


class SubscriptionPreprocessMode(Enum):
    """The modes that can be used for the :meth:`Session.subscribe()` and
    :meth:`Session.resubscribe()` operations."""

    FAIL_ON_FIRST_ERROR = 1
    """In this mode, an exception is raised on the first invalid entry in the
    :class:`SubscriptionList`, and the entire batch will be considered failed.
    Otherwise, all the subscriptions will move forward and the method returns
    ``None``."""

    RETURN_INDIVIDUAL_ERRORS = 2
    """In this mode, instead of raising exceptions, a list of
    :class:`SubscriptionPreprocessError` is returned, each representing an
    error due to an invalid subscription in the :class:`SubscriptionList`. The
    valid subscriptions will move forward."""


class Session(AbstractSession, metaclass=MetaClassForClassesWithEnums):
    """Consumer session for making requests for Bloomberg services.

    This class provides a consumer session for making requests for Bloomberg
    services. For information on generic session operations, see the parent
    class: :class:`AbstractSession`.

    Sessions manage access to services either by requests and responses or
    subscriptions. A Session can dispatch events and replies in either a
    synchronous or asynchronous mode. The mode of a Session is determined when
    it is constructed and cannot be changed subsequently.

    A Session is asynchronous if an ``eventHandler`` argument is supplied when
    it is constructed. The ``nextEvent()`` method may not be called.  All
    incoming events are delivered to the ``eventHandler`` supplied on
    construction.

    If supplied, ``eventHandler`` must be a callable object that takes two
    arguments: received :class:`Event` and related session.

    A Session is synchronous if an ``eventHandler`` argument is not supplied
    when it is constructed. The :meth:`nextEvent()` method must be called to
    read incoming events.

    Several methods in Session take a :class:`CorrelationId` parameter. The
    application may choose to supply its own :class:`CorrelationId` values or
    allow the Session to create values. If the application supplies its own
    :class:`CorrelationId` values it must manage their lifetime such that the
    same value is not reused for more than one operation at a time. The
    lifetime of a :class:`CorrelationId` begins when it is supplied in a method
    invoked on a Session and ends either when it is explicitly cancelled using
    :meth:`cancel()` or :meth:`unsubscribe()`, when a :attr:`~Event.RESPONSE`
    :class:`Event` (not a :attr:`~Event.PARTIAL_RESPONSE`) containing it is
    received or when a :attr:`~Event.SUBSCRIPTION_STATUS` :class:`Event` which
    indicates that the subscription it refers to has been terminated is
    received.

    When using an asynchronous Session, the application must be aware that
    because the callbacks are generated from another thread, they may be
    processed before the call which generates them has returned. For example,
    the :attr:`~Event.SESSION_STATUS` :class:`Event` generated by a
    :meth:`startAsync()` may be processed before :meth:`startAsync()` has
    returned (even though :meth:`startAsync()` itself will not block).

    This becomes more significant when Session generated
    :class:`CorrelationId`\ s are in use. For example, if a call to
    :meth:`subscribe()` which returns a Session generated
    :class:`CorrelationId` has not completed before the first :class:`Event`\ s
    which contain that :class:`CorrelationId` arrive the application may not be
    able to interpret those events correctly. For this reason, it is preferable
    to use user generated :class:`CorrelationId`\ s when using asynchronous
    Sessions. This issue does not arise when using a synchronous Session as
    long as the calls to :meth:`subscribe()` etc. are made on the same thread
    as the calls to :meth:`nextEvent()`.

    The class attributes represent the states in which a subscription can be.
    """

    UNSUBSCRIBED = internals.SUBSCRIPTIONSTATUS_UNSUBSCRIBED
    """No longer active, terminated by API."""
    SUBSCRIBING = internals.SUBSCRIPTIONSTATUS_SUBSCRIBING
    """Initiated but no updates received."""
    SUBSCRIBED = internals.SUBSCRIPTIONSTATUS_SUBSCRIBED
    """Updates are flowing."""
    CANCELLED = internals.SUBSCRIPTIONSTATUS_CANCELLED
    """No longer active, terminated by Application."""
    PENDING_CANCELLATION = internals.SUBSCRIPTIONSTATUS_PENDING_CANCELLATION
    """No longer active, terminated by Application."""

    __handle = None  # pylint: disable=unused-private-member
    __handlerProxy = None  # pylint: disable=unused-private-member

    @staticmethod
    def __dispatchEvent(
        sessionRef: "ReferenceType[Session]", eventHandle: BlpapiEventHandle
    ) -> None:  # pragma: no cover
        """event dispatcher"""
        try:
            session = sessionRef()
            if session is not None:
                event = Event(eventHandle, {session})
                session.__handler(event, session)
        except:
            print("Exception in event handler:", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            os._exit(1)

    def __init__(
        self,
        options: Optional[SessionOptions] = None,
        eventHandler: Optional[Callable[[Event, Session], None]] = None,
        eventDispatcher: Optional["typehints.EventDispatcher"] = None,
    ) -> None:
        """Create a consumer :class:`Session`.

        Args:
            options: Options to construct the session with
            eventHandler: Handler for events
                generated by the session. Takes two arguments - received event
                and related session
            eventDispatcher: An optional dispatcher for events.

        Raises:
            InvalidArgumentException: If ``eventHandler`` is ``None`` and and
                the ``eventDispatcher`` is not ``None``

        If ``eventHandler`` is not ``None`` then this :class:`Session` will
        operate in asynchronous mode, otherwise the :class:`Session` will
        operate in synchronous mode.

        If ``eventDispatcher`` is ``None`` then the :class:`Session` will
        create a default :class:`EventDispatcher` for this :class:`Session`
        which will use a single thread for dispatching events. For more control
        over event dispatching a specific instance of :class:`EventDispatcher`
        can be supplied. This can be used to share a single
        :class:`EventDispatcher` amongst multiple :class:`Session` objects.

        If an ``eventDispatcher`` is supplied which uses more than one thread
        the :class:`Session` will ensure that events which should be ordered
        are passed to callbacks in a correct order. For example, partial
        response to a request or updates to a single subscription.

        Each ``eventDispatcher`` uses it's own thread or pool of threads so if
        you want to ensure that a session which receives very large messages
        and takes a long time to process them does not delay a session that
        receives small messages and processes each one very quickly then give
        each one a separate ``eventDispatcher``.

        Note:
            In case of unhandled exception in ``eventHandler``, the exception
            traceback will be printed to ``sys.stderr`` and application will be
            terminated with nonzero exit code.
        """
        if (eventHandler is None) and (eventDispatcher is not None):
            raise exception.InvalidArgumentException(
                "eventDispatcher is specified but eventHandler is None", 0
            )
        if options is None:
            options = SessionOptions()
        if eventHandler is not None:
            # pylint: disable=unused-private-member
            self.__handler = eventHandler
            self.__handlerProxy = functools.partial(
                Session.__dispatchEvent, ref(self)
            )
        self.__handle = internals.Session_createHelper(
            get_handle(options),
            self.__handlerProxy,
            get_handle(eventDispatcher),
        )

        _destroy = internals.Session_destroyHelper
        # note: AbstractSession destroy passes AbstractSession handle
        _dtor = lambda hndl: _destroy(self.__handle, self.__handlerProxy)
        atexit.register(self.stop)  # we must stop session before shutdown

        AbstractSession.__init__(
            self,
            internals.blpapi_Session_getAbstractSession(self.__handle),
            _dtor,
        )

    def start(self) -> bool:
        """Start this :class:`Session` in synchronous mode.

        Returns:
            ``True`` if the :class:`Session` started successfully,
            ``False`` otherwise.

        Attempt to start this :class:`Session` and block until the
        :class:`Session` has started or failed to start.  Before
        :meth:`start()` returns a :attr:`~Event.SESSION_STATUS` :class:`Event`
        is generated. A :class:`Session` may only be started once.
        """
        return internals.blpapi_Session_start(self.__handle) == 0

    def startAsync(self) -> bool:
        """Start this :class:`Session` in asynchronous mode.

        Returns:
            ``True`` if the process to start a :class:`Session` began
            successfully, ``False`` otherwise.

        Attempt to begin the process to start this :class:`Session`. The
        application must monitor events for a :attr:`~Event.SESSION_STATUS`
        :class:`Event` which will be generated once the :class:`Session` has
        started or if it fails to start. The :attr:`~Event.SESSION_STATUS`
        :class:`Event` may be processed by the registered ``eventHandler``
        before :meth:`startAsync()` has returned. A :class:`Session` may only
        be started once.
        """
        return internals.blpapi_Session_startAsync(self.__handle) == 0

    def stop(self) -> bool:
        """Stop operation of this :class:`Session` and wait until it stops.

        Returns:
            ``True`` if the :class:`Session` stopped successfully,
            ``False`` otherwise.

        Stop operation of this :class:`Session` and block until all callbacks
        to ``eventHandler`` objects relating to this :class:`Session` which are
        currently in progress have completed (including the callback to handle
        the :class:`~Event.SESSION_STATUS` :class:`Event` this call generates).
        Once this returns no further callbacks to ``eventHandlers`` will occur.
        If :meth:`stop()` is called from within an ``eventHandler`` callback it
        is silently converted to a :meth:`stopAsync()` in order to prevent
        deadlock. Once a :class:`Session` has been stopped it can only be
        destroyed.
        """
        atexit.unregister(self.stop)
        return internals.blpapi_Session_stop(self.__handle) == 0

    def stopAsync(self) -> bool:
        """Begin the process to stop this Session and return immediately.

        Returns:
            ``True`` if the process to stop a :class:`Session` began
            successfully, ``False`` otherwise.

        The application must monitor events for a :attr:`~Event.SESSION_STATUS`
        :class:`Event` which will be generated once the :class:`Session` has
        been stopped. After this :attr:`~Event.SESSION_STATUS` :class:`Event`
        no further callbacks to ``eventHandlers`` will occur. Once a
        :class:`Session` has been stopped it can only be destroyed.
        """
        return internals.blpapi_Session_stopAsync(self.__handle) == 0

    def nextEvent(self, timeout: int = 0) -> Event:
        """
        Args:
            timeout: Timeout threshold in milliseconds

        Returns:
            Event: Next available event for this session

        Raises:
            InvalidStateException: If invoked on a session created in
                asynchronous mode

        If there is no :class:`Event` available this will block for up to the
        specified ``timeout`` milliseconds for an :class:`Event` to arrive. A
        value of ``0`` for ``timeout`` (the default) indicates
        :meth:`nextEvent()` should not timeout and will not return until the
        next :class:`Event` is available.

        If :meth:`nextEvent()` returns due to a timeout it will return an event
        of type :attr:`~Event.TIMEOUT`.
        """
        retCode, event = internals.blpapi_Session_nextEvent(
            self.__handle, timeout
        )

        _ExceptionUtil.raiseOnError(retCode)

        return Event(event, {self})

    def tryNextEvent(self) -> Optional[Event]:
        """
        Returns:
            Event: Next available event for this session

        If there are :class:`Event`\ s available for the session, return the
        next :class:`Event` If there is no event available for the
        :class:`Session`, return ``None``. This method never blocks.
        """
        retCode, event = internals.blpapi_Session_tryNextEvent(self.__handle)
        if retCode:
            return None
        return Event(event, {self})

    @staticmethod
    def _createErrorAppender(
        errorList: List[SubscriptionPreprocessError],
    ) -> Callable:
        def errorAppender(
            correlationId: CorrelationId,
            subscriptionString: str,
            errorCode: int,
            description: str,
        ) -> None:
            errorList.append(
                SubscriptionPreprocessError(
                    correlationId,
                    subscriptionString,
                    SubscriptionPreprocessError.ErrorCode(errorCode),
                    description,
                )
            )

        return errorAppender

    def subscribe(
        self,
        subscriptionList: "typehints.SubscriptionList",
        identity: Optional["typehints.Identity"] = None,
        requestLabel: str = "",
        mode: SubscriptionPreprocessMode = SubscriptionPreprocessMode.FAIL_ON_FIRST_ERROR,
    ) -> Optional[List[SubscriptionPreprocessError]]:
        """Begin subscriptions for each entry in the specified list.

        Args:
            subscriptionList: List of subscriptions to begin
            identity: Identity used for authorization
            requestLabel: String which will be recorded along with any
                diagnostics for this operation
            mode: Mode to use for this operation.
                See :class:`SubscriptionPreprocessMode` for an explanation of
                the available modes.

        Returns:
            If mode is :attr:`~SubscriptionPreprocessMode.FAIL_ON_FIRST_ERROR`,
                then ``None`` is returned. If mode is
                :attr:`~SubscriptionPreprocessMode.RETURN_INDIVIDUAL_ERRORS`,
                then a list of :class:`SubscriptionPreprocessError` is
                returned.

        Begin subscriptions for each entry in the specified
        ``subscriptionList``, which must be an object of type
        :class:`SubscriptionList`, optionally using the specified ``identity``
        for authorization. If no ``identity`` is specified, the default
        authorization information is used.  If the optional ``requestLabel`` is
        provided it defines a string which will be recorded along with any
        diagnostics for this operation.

        A :attr:`~Event.SUBSCRIPTION_STATUS` :class:`Event` will be generated
        for each entry in the ``subscriptionList``.

        When ``identity`` is not provided, the session identity will be used if
        it has been authorized.
        """
        subMode = SubscriptionPreprocessMode(mode)

        if subMode == SubscriptionPreprocessMode.FAIL_ON_FIRST_ERROR:
            _ExceptionUtil.raiseOnError(
                internals.blpapi_Session_subscribe(
                    self.__handle,
                    get_handle(subscriptionList),
                    get_handle(identity),
                    requestLabel,
                )
            )
        elif subMode == SubscriptionPreprocessMode.RETURN_INDIVIDUAL_ERRORS:
            errorList: List[SubscriptionPreprocessError] = []
            errorAppender = Session._createErrorAppender(errorList)
            _ExceptionUtil.raiseOnError(
                internals.blpapi_Session_subscribeEx_helper(
                    self.__handle,
                    get_handle(subscriptionList),
                    get_handle(identity),
                    requestLabel,
                    errorAppender,
                )
            )

            return errorList
        else:
            raise exception.InvalidArgumentException(
                "Unsupported SubscriptionPreprocessMode: '{}'".format(mode), 0
            )
        return None

    def unsubscribe(
        self, subscriptionList: "typehints.SubscriptionList"
    ) -> None:
        """Cancel subscriptions from the specified ``subscriptionList``.

        Args:
            subscriptionList: List of subscriptions to cancel

        Cancel each of the current subscriptions identified by the specified
        ``subscriptionList``, which must be an object of type
        :class:`SubscriptionList`.  If the correlation ID of any entry in the
        ``subscriptionList`` does not identify a current subscription then that
        entry is ignored. All entries which have valid correlation IDs will be
        cancelled.

        Once this call returns the correlation ids in the ``subscriptionList``
        will not be seen in any subsequent :class:`Message` obtained from a
        ``MessageIterator`` by calling ``next()`` However, any :class:`Message`
        currently pointed to by a ``MessageIterator`` when
        :meth:`unsubscribe()` is called is not affected even if it has one of
        the correlation IDs in the ``subscriptionList``. Also any
        :class:`Message` where a reference has been retained by the application
        may still contain a correlation ID from the ``subscriptionList``. For
        these reasons, although technically an application is free to re-use
        the correlation IDs as soon as this method returns it is preferable not
        to aggressively re-use correlation IDs, particularly with an
        asynchronous :class:`Session`.
        """
        _ExceptionUtil.raiseOnError(
            internals.blpapi_Session_unsubscribe(
                self.__handle, get_handle(subscriptionList), None
            )
        )

    def resubscribe(
        self,
        subscriptionList: "typehints.SubscriptionList",
        requestLabel: str = "",
        resubscriptionId: Optional[int] = None,
        mode: SubscriptionPreprocessMode = SubscriptionPreprocessMode.FAIL_ON_FIRST_ERROR,
    ) -> Optional[List[SubscriptionPreprocessError]]:
        """Modify subscriptions in ``subscriptionList``.

        Args:
            subscriptionList: List of subscriptions to modify
            requestLabel: String which will be recorded along with any
                diagnostics for this operation
            resubscriptionId: An id that will be included in the event
                generated from this operation
            mode: Mode to use for this operation.
                See :class:`SubscriptionPreprocessMode` for an explanation of
                the available modes.

        Returns:
            If mode is :attr:`~SubscriptionPreprocessMode.FAIL_ON_FIRST_ERROR`,
                then ``None`` is returned. If mode is
                :attr:`~SubscriptionPreprocessMode.RETURN_INDIVIDUAL_ERRORS`,
                then a list of :class:`SubscriptionPreprocessError` is
                returned.

        Modify each subscription in the specified ``subscriptionList``, which
        must be an object of type :class:`SubscriptionList`, to reflect the
        modified options specified for it. If the optional ``requestLabel`` is
        provided it defines a string which will be recorded along with any
        diagnostics for this operation.

        For each entry in the ``subscriptionList`` which has a correlation ID
        which identifies a current subscription the modified options replace
        the current options for the subscription and a
        :attr:`~Event.SUBSCRIPTION_STATUS` :class:`Event` (containing the
        ``resubscriptionId`` if specified) will be generated in the event
        stream before the first update based on the new options. If the
        correlation ID of an entry in the ``subscriptionList`` does not
        identify a current subscription then that entry is ignored.
        """
        subMode = SubscriptionPreprocessMode(mode)

        if subMode == SubscriptionPreprocessMode.FAIL_ON_FIRST_ERROR:
            if resubscriptionId is None:
                _ExceptionUtil.raiseOnError(
                    internals.blpapi_Session_resubscribe(
                        self.__handle,
                        get_handle(subscriptionList),
                        requestLabel,
                    )
                )
            else:
                _ExceptionUtil.raiseOnError(
                    internals.blpapi_Session_resubscribeWithId(
                        self.__handle,
                        get_handle(subscriptionList),
                        resubscriptionId,  # an int, not a cid
                        requestLabel,
                    )
                )
        elif subMode == SubscriptionPreprocessMode.RETURN_INDIVIDUAL_ERRORS:
            errorList: List[SubscriptionPreprocessError] = []
            errorAppender = Session._createErrorAppender(errorList)

            if resubscriptionId is None:
                _ExceptionUtil.raiseOnError(
                    internals.blpapi_Session_resubscribeEx_helper(
                        self.__handle,
                        get_handle(subscriptionList),
                        requestLabel,
                        errorAppender,
                    )
                )
            else:
                _ExceptionUtil.raiseOnError(
                    internals.blpapi_Session_resubscribeWithIdEx_helper(
                        self.__handle,
                        get_handle(subscriptionList),
                        resubscriptionId,  # an int, not a cid
                        requestLabel,
                        errorAppender,
                    )
                )

            return errorList
        else:
            raise exception.InvalidArgumentException(
                "Unsupported SubscriptionPreprocessMode: '{}'".format(mode), 0
            )
        return None

    def setStatusCorrelationId(
        self,
        service: "typehints.Service",
        correlationId: CorrelationId,
        identity: Optional["typehints.Identity"] = None,
    ) -> None:
        """Set the Correlation id on which service status messages will be
        received.

        Args:
            service: The service from which the status messages
                are received
            correlationId: Correlation id to associate with the
                service status messages
            identity: Identity used for authorization

        Note:
            No service status messages are received prior to this call
        """
        _ExceptionUtil.raiseOnError(
            internals.blpapi_Session_setStatusCorrelationId(
                self.__handle,
                get_handle(service),
                get_handle(identity),
                correlationId,
            )
        )

    def sendRequest(
        self,
        request: "typehints.Request",
        identity: Optional["typehints.Identity"] = None,
        correlationId: Optional[CorrelationId] = None,
        eventQueue: Optional["typehints.EventQueue"] = None,
        requestLabel: str = "",
    ) -> CorrelationId:
        """Send the specified ``request``.

        Args:
            request: Request to send
            identity: Identity used for authorization
            correlationId: Correlation id to associate with the
                request
            eventQueue: Event queue on which the events related to
                this operation will arrive
            requestLabel: String which will be recorded along with any
                diagnostics for this operation

        Returns:
            CorrelationId: The actual correlation id associated with the
            request

        Send the specified ``request`` using the optionally specified
        ``identity`` for authorization. If ``identity`` is not provided, then
        the request will be sent using the session identity. If the optionally
        specified ``correlationId`` is supplied use it, otherwise create a
        :class:`CorrelationId`. The actual :class:`CorrelationId` used is
        returned. If the optionally specified ``eventQueue`` is supplied all
        events relating to this :class:`Request` will arrive on that
        :class:`EventQueue`. If the optional ``requestLabel`` is provided it
        defines a string which will be recorded along with any diagnostics for
        this operation.

        A successful request will generate zero or more
        :class:`~Event.PARTIAL_RESPONSE` :class:`Message`\ s followed by
        exactly one :class:`~Event.RESPONSE` :class:`Message`. Once the final
        :class:`~Event.RESPONSE` :class:`Message` has been received the
        :class:`CorrelationId` associated with this request may be re-used. If
        the request fails at any stage a :class:`~Event.REQUEST_STATUS` will be
        generated after which the :class:`CorrelationId` associated with the
        request may be re-used.

        When ``identity`` is not provided, the session identity will be used if
        it has been authorized.
        """
        if correlationId is None:
            correlationId = CorrelationId()
        res = internals.blpapi_Session_sendRequest(
            self.__handle,
            get_handle(request),
            correlationId,
            get_handle(identity),
            get_handle(eventQueue),
            requestLabel,
        )
        _ExceptionUtil.raiseOnError(res)
        if eventQueue is not None:
            eventQueue._registerSession(self)
        return correlationId

    def sendRequestTemplate(
        self,
        requestTemplate: RequestTemplate,
        correlationId: Optional[CorrelationId] = None,
    ) -> CorrelationId:
        """Send a request defined by the specified ``requestTemplate``.

        Args:
            requestTemplate: Template that defines the
                request
            correlationId: Correlation id to associate with the
                request

        Returns:
            CorrelationId: The actual correlation id used for the request is
            returned.

        If the optionally specified ``correlationId`` is supplied, use it
        otherwise create a new :class:`CorrelationId`. The actual
        :class:`CorrelationId` used is returned.

        A successful request will generate zero or more
        :attr:`~Event.PARTIAL_RESPONSE` events followed by exactly one
        :attr:`~Event.RESPONSE` event. Once the final :attr:`~Event.RESPONSE`
        event has been received the ``CorrelationId`` associated with  this
        request may be re-used. If the request fails at any stage a
        :attr:`~Event.REQUEST_STATUS` will be generated after which the
        ``CorrelationId`` associated with the request may be re-used.
        """
        if correlationId is None:
            correlationId = CorrelationId()
        res = internals.blpapi_Session_sendRequestTemplate(
            self.__handle, get_handle(requestTemplate), correlationId
        )
        _ExceptionUtil.raiseOnError(res)
        return correlationId

    def createSnapshotRequestTemplate(
        self,
        subscriptionString: str,
        correlationId: CorrelationId,
        identity: Optional["typehints.Identity"] = None,
    ) -> RequestTemplate:
        """Create a snapshot request template for getting subscription data
        specified by the ``subscriptionString`` using the specified
        ``identity``.

        Args:
            subscriptionString: String that specifies the subscription
            correlationId: Correlation id to associate with
                events generated by this operation
            identity: Optional. Identity used for authorization.

        Returns:
            RequestTemplate: Created request template

        Raises:
            Exception: If one or more of the following conditions is not met:
                the session is established, ``subscriptionString`` is a valid
                subscription string and ``correlationId`` is not used in this
                session.

        The provided ``correlationId`` will be used for status updates about
        the created request template state and an implied subscription
        associated with it delivered by :attr:`~Event.SUBSCRIPTION_STATUS`
        events.

        The benefit of the snapshot request templates is that these requests
        may be serviced from a cache and the user may expect to see
        significantly lower response time.

        There are 3 possible states for a created request template:
        ``Pending``, ``Available``, and ``Terminated``. Right after creation a
        request template is in the ``Pending`` state.

        If a state is ``Pending``, the user may send a request using this
        template but there are no guarantees about response time since cache
        is not available yet. Request template may transition into ``Pending``
        state only from the ``Available`` state. In this case the
        ``RequestTemplatePending`` message is generated.

        If state is ``Available``, all requests will be serviced from a cache
        and the user may expect to see significantly reduced latency. Note,
        that a snapshot request template can transition out of the
        ``Available`` state concurrently with requests being sent, so no
        guarantee of service from the cache can be provided. Request
        template may transition into ``Available`` state only from the
        ``Pending`` state. In this case the ``RequestTemplateAvailable`` message
        is generated. This message will also contain information about
        currently used connection in the ``boundTo`` field. Note that it is
        possible to get the ``RequestTemplateAvailable`` message with a new
        connection information, even if a request template is already in the
        ``Available`` state.

        If state is ``Terminated``, sending request will always result in a
        failure response. Request template may transition into this state from
        any other state. This is a final state and it is guaranteed that the
        last message associated with the provided ``correlationId`` will be the
        ``RequestTemplateTerminated`` message which is generated when a request
        template transitions into this state. If a request template transitions
        into this state, all outstanding requests will be failed and
        appropriate messages will be generated for each request. After
        receiving the ``RequestTemplateTerminated`` message, ``correlationId``
        may be reused.

        Note that resources used by a snapshot request template are released
        only when request template transitions into the ``Terminated`` state
        or when session is destroyed. In order to release resources when
        request template is not needed anymore, user should call the
        :meth:`cancel()` method unless the ``RequestTemplateTerminated``
        message was already received due to some problems. When the last
        copy of a ``RequestTemplate`` object goes out of scope and there are
        no outstanding requests left, the snapshot request template will be
        destroyed automatically. If the last copy of a ``RequestTemplate``
        object goes out of scope while there are still some outstanding
        requests left, snapshot service request template will be destroyed
        automatically when the last request gets a final response.

        When ``identity`` is ``None``, the session identity will be used if it
        has been authorized.
        """

        cidArg = correlationId
        identityArg = identity

        # we changed the order of last two arguments
        # old clients may have them swapped at call site.
        # This detects the swap and calls the method correctly.

        # Note: cid may never be None, only identity is allowed None
        # Hence, in the swapped case identity must be of type CorrelationId
        if isinstance(identity, CorrelationId):
            cidArg = identity
            identityArg = correlationId

        rc, template = internals.blpapi_Session_createSnapshotRequestTemplate(
            self.__handle, subscriptionString, get_handle(identityArg), cidArg
        )
        _ExceptionUtil.raiseOnError(rc)
        reqTemplate = RequestTemplate(template)
        return reqTemplate


class SubscriptionPreprocessError:
    """Class representing an error due to an invalid subscription."""

    class ErrorCode(Enum):
        """The error codes used by :class:`SubscriptionPreprocessError`."""

        SUBSCRIPTIONPREPROCESS_INVALID_SUBSCRIPTION_STRING = (
            internals.SUBSCRIPTIONPREPROCESS_INVALID_SUBSCRIPTION_STRING
        )
        """Error due to an invalid subscription string."""

        SUBSCRIPTIONPREPROCESS_CORRELATIONID_ERROR = (
            internals.SUBSCRIPTIONPREPROCESS_CORRELATIONID_ERROR
        )
        """Error due to misuse of correlation id, such as using a duplicate."""

    def __init__(
        self,
        correlationId: CorrelationId,
        subscriptionString: str,
        errorCode: SubscriptionPreprocessError.ErrorCode,
        description: str,
    ) -> None:
        self.correlationId = correlationId
        self.subscriptionString = subscriptionString
        self.errorCode = errorCode
        self.description = description

    def __str__(self) -> str:
        return (
            f"{{correlationId: {self.correlationId}"
            f", subscriptionString: {self.subscriptionString}"
            f", code: {self.errorCode}"
            f", description: {self.description}}}"
        )


__copyright__ = """
Copyright 2012. Bloomberg Finance L.P.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to
deal in the Software without restriction, including without limitation the
rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
sell copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:  The above
copyright notice and this permission notice shall be included in all copies
or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
IN THE SOFTWARE.
"""
