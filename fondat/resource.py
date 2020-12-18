"""
Module to implement resources.

A resource is an addressible object that exposes operations through a uniform
set of methods.

A resource class contains operation methods, each decorated with the
@operation decorator.
"""

import asyncio
import functools
import http
import inspect
import fondat.context as context
import fondat.monitor as monitor
import fondat.validate
import types
import wrapt

from collections.abc import Iterable
from fondat.security import SecurityRequirement
from typing import Any


class ResourceError(Exception):
    """Base class for resource errors."""

    pass


# generate concrete error classes
for status in http.HTTPStatus:
    if 400 <= status <= 599:
        name = "".join([w.capitalize() for w in status.name.split("_")])
        globals()[name] = type(name, (ResourceError,), {"status": status})


def _summary(function):
    """
    Derive summary information from a function's docstring or name. The summary is
    the first sentence of the docstring, ending in a period, or if no dostring is
    present, the function's name capitalized.
    """
    if not function.__doc__:
        return f"{function.__name__.capitalize()}."
    result = []
    for word in function.__doc__.split():
        result.append(word)
        if word.endswith("."):
            break
    return " ".join(result)


async def authorize(security):
    """
    Peform authorization of an operation.

    Parameters:
    • security: Iterable of security requirements.

    This coroutine executes the security requirements. If any security
    requirement does not raise an exception then this coroutine passes and
    authorization is granted.

    If one security requirement raises a Forbidden exception, then a Forbidden
    exception will be raised; otherwise an Unauthorized exception will be
    raised. If a non-security exception is raised, then it is re-raised.
    """
    exception = None
    for requirement in security or []:
        try:
            await requirement.authorize()
            return  # security requirement authorized the operation
        except Forbidden:
            exception = Forbidden
        except Unauthorized:
            if not exception:
                exception = Unauthorized
        except:
            raise
    if exception:
        raise exception


def resource(wrapped=None, *, tag=None):
    """
    Decorate a class to be a resource containing operations.

    Parameters:
    • tag: Tag to group resources.  [resource class name in lower case]
    """

    if wrapped is None:
        return functools.partial(resource, tag=tag)

    wrapped._fondat_resource = types.SimpleNamespace(
        tag=tag or wrapped.__name__.lower()
    )

    return wrapped


def is_resource(obj_or_type: Any):
    """Return if object or type represents a resource."""
    return getattr(obj_or_type, "_fondat_resource", None) is not None


def is_operation(obj_or_type: Any):
    """Return if object represents a resource operation."""
    return getattr(obj_or_type, "_fondat_operation", None) is not None


def operation(
    wrapped=None,
    *,
    op_type: str = None,
    security: Iterable[SecurityRequirement] = None,
    publish: bool = True,
    deprecated: bool = False,
    validate: bool = True,
):
    """
    Decorate a resource coroutine that performs an operation.

    Parameters:
    • op_type: Operation type.  {"query", "mutation"}
    • security: Security requirements for the operation.
    • publish: Publish the operation in documentation.
    • deprecated: Declare the operation as deprecated.

    Resource operations should correlate to HTTP method names, named in lower
    case. For example: get, put, post, delete, patch.
    """

    if wrapped is None:
        return functools.partial(
            operation,
            security=security,
            publish=publish,
            deprecated=deprecated,
        )

    if not asyncio.iscoroutinefunction(wrapped):
        raise TypeError("operation must be a coroutine")

    op_type = op_type or "query" if wrapped.__name__ == "get" else "mutation"
    name = wrapped.__name__
    description = wrapped.__doc__ or name
    summary = _summary(wrapped)

    for p in inspect.signature(wrapped).parameters.values():
        if p.kind is p.VAR_POSITIONAL:
            raise TypeError("operation with *args is not supported")
        elif p.kind is p.VAR_KEYWORD:
            raise TypeError("operation with **kwargs is not supported")

    @wrapt.decorator
    async def wrapper(wrapped, instance, args, kwargs):
        operation = getattr(wrapped, "_fondat_operation")
        tags = {
            "resource": f"{instance.__class__.__module__}.{instance.__class__.__qualname__}",
            "operation": wrapped.__name__,
        }
        with context.push({"context": "fondat.operation", **tags}):
            async with monitor.timer({"name": "operation_duration_seconds", **tags}):
                async with monitor.counter({"name": "operation_calls_total", **tags}):
                    await authorize(operation.security)
                    return await wrapped(*args, **kwargs)

    wrapped._fondat_operation = types.SimpleNamespace(
        op_type=op_type,
        summary=summary,
        description=description,
        security=security,
        publish=publish,
        deprecated=deprecated,
    )

    if validate:
        wrapped = fondat.validate.validate_arguments(wrapped)

    return wrapper(wrapped)


def inner(
    wrapped=None,
    *,
    method: str,
    op_type: str = None,
    security: Iterable[SecurityRequirement] = None,
):
    """
    Decorator to define an inner resource operation.

    Parameters:
    • method: Name of method to implement (e.g "get").
    • security: Security requirements for the operation.

    This decorator creates a new resource class, with a single operation that
    implements the decorated method. The decorated method, at time of
    invocation, is bound to the original outer resource instance.
    """

    if wrapped is None:
        return functools.partial(
            inner,
            method=method,
            op_type=op_type,
            security=security,
        )

    if not asyncio.iscoroutinefunction(wrapped):
        raise TypeError("inner resource method must be a coroutine")

    if not method:
        raise TypeError("method name is required")

    _wrapped = fondat.validate.validate_arguments(wrapped)

    @resource
    class Inner:
        def __init__(self, outer):
            self.outer = outer

    Inner.__doc__ = wrapped.__doc__
    Inner.__name__ = wrapped.__name__.title().replace("_", "")
    Inner.__qualname__ = Inner.__name__
    Inner.__module__ = wrapped.__module__

    async def proxy(self, *args, **kwargs):
        return await types.MethodType(_wrapped, self.outer)(*args, **kwargs)

    functools.update_wrapper(proxy, wrapped)
    proxy.__name__ = method
    proxy = operation(proxy, security=security, validate=False)
    setattr(Inner, method, proxy)
    setattr(Inner, "__call__", proxy)

    def res(self):
        return Inner(self)

    res.__doc__ = wrapped.__doc__
    res.__module__ = wrapped.__module__
    res.__name__ = wrapped.__name__
    res.__qualname__ = wrapped.__qualname__
    res.__annotations__ = {"return": Inner}

    return property(res)


def query(wrapped, *, method: str = "get", **kwargs):
    """Decorator to define an inner resource query operation."""

    if wrapped is None:
        return functools.partial(
            query,
            method=method,
            **kwargs,
        )

    return inner(wrapped, op_type="query", method=method, **kwargs)


def mutation(wrapped, *, method: str = "post", **kwargs):
    """Decorator to define an inner resource mutation operation."""

    if wrapped is None:
        return functools.partial(
            mutation,
            method=method,
            **kwargs,
        )

    return inner(wrapped, op_type="mutation", method=method, **kwargs)
