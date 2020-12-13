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
import fondat.enum
import fondat.monitor as monitor
import fondat.validate
import types
import wrapt


class ResourceError(Exception):
    """Base class for resource errors."""

    pass


In = fondat.enum.str_enum("In", "query header body")


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


_methods = {
    "get": "query",
    "put": "mutation",
    "post": "mutation",
    "delete": "mutation",
    "patch": "mutation",
}


_operation_types = {"query", "mutation"}


def operation(
    wrapped=None,
    *,
    type=None,
    security=None,
    publish=True,
    deprecated=False,
):
    """
    Decorate a resource coroutine that performs an operation.

    Parameters:
    • type: Type of operation.  {"query", "mutation"}
    • security: Security requirements for the operation.
    • publish: Publish the operation in documentation.
    • deprecated: Declare the operation as deprecated.

    If method name is "get", then type defaults to "query"; if name is one of
    {"put", "post", "delete", "patch"} then type defaults to "mutation";
    otherwise type must be specified.
    """

    if wrapped is None:
        return functools.partial(
            operation,
            type=type,
            security=security,
            publish=publish,
            deprecated=deprecated,
        )

    if not asyncio.iscoroutinefunction(wrapped):
        raise TypeError("operation must be a coroutine")

    name = wrapped.__name__
    description = wrapped.__doc__ or name
    summary = _summary(wrapped)

    _type = type or (_methods.get(name))

    if _type not in _operation_types:
        raise ValueError(f"operation type must be one of: {_operation_types}")

    for p in inspect.signature(wrapped).parameters.values():
        if p.kind is p.VAR_POSITIONAL:
            raise ValueError("function with *args unsupported")
        elif p.kind is p.VAR_KEYWORD:
            raise ValueError("function with **kwargs unsupported")

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
        type=_type,
        summary=summary,
        description=description,
        security=security,
        publish=publish,
        deprecated=deprecated,
    )

    wrapped = fondat.validate.validate_arguments(wrapped)
    #    wrapped = fondat.validate.validate_return_value(wrapped)
    return wrapper(wrapped)
