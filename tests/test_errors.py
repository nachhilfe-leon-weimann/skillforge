from app.core.errors import ConflictError, DomainError, DomainValidationError, NotFoundError, UpstreamError


class WidgetNotFoundError(NotFoundError):
    message = "Widget not found"


class HTTPGatewayTimeoutV2Error(DomainError):
    pass


class RenamedWidgetError(ConflictError):
    code = "widget_locked"


class ChildOfRenamedError(RenamedWidgetError):
    pass


def test_code_is_derived_from_the_class_name():
    assert WidgetNotFoundError.code == "widget_not_found"


def test_code_derivation_keeps_acronyms_and_digits_together():
    assert HTTPGatewayTimeoutV2Error.code == "http_gateway_timeout_v2"


def test_explicit_code_wins_over_the_class_name():
    assert RenamedWidgetError.code == "widget_locked"


def test_explicit_code_is_not_inherited_by_subclasses():
    assert ChildOfRenamedError.code == "child_of_renamed"


def test_categories_carry_their_own_code_and_a_safe_default_message():
    for category in (DomainError, NotFoundError, ConflictError, DomainValidationError, UpstreamError):
        assert category.code
        assert category.message
        assert category.expose_message is False

    assert NotFoundError.code == "not_found"
    assert ConflictError.code == "conflict"


def test_subclass_inherits_the_category_message_unless_it_sets_its_own():
    assert ChildOfRenamedError.message == ConflictError.message
    assert WidgetNotFoundError.message == "Widget not found"


def test_instance_message_stays_available_to_the_service_layer():
    error = WidgetNotFoundError("widget 7f3a is gone")

    assert str(error) == "widget 7f3a is gone"
    assert isinstance(error, DomainError)
