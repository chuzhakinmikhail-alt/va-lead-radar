"""Tests for contact extraction, the fallback when a posting has no contacts."""

from app.contacts import extract_contacts


def test_extracts_telegram_handle_and_email():
    contacts = extract_contacts("Ищу разработчика, лс @dev_ivan или ivan@example.com")
    assert contacts.telegram == ["@dev_ivan"]
    assert contacts.email == ["ivan@example.com"]
    assert contacts.found


def test_email_local_part_is_not_reported_as_a_handle():
    contacts = extract_contacts("пишите на support@company.com")
    assert contacts.telegram == []
    assert contacts.email == ["support@company.com"]


def test_extracts_tme_link_as_handle():
    contacts = extract_contacts("связь: https://t.me/bot_maker")
    assert contacts.telegram == ["@bot_maker"]


def test_extracts_russian_phone_variants():
    assert extract_contacts("тел +7 999 123-45-67").phone == ["+7 999 123-45-67"]
    assert extract_contacts("8 (912) 345-67-89").phone == ["8 (912) 345-67-89"]
    assert extract_contacts("звоните 89123456789").phone == ["89123456789"]


def test_budget_numbers_are_not_phones():
    assert extract_contacts("бюджет 500000 руб, срок 2 недели").phone == []


def test_deduplicates_repeated_contacts():
    contacts = extract_contacts("@dev_ivan @dev_ivan @dev_ivan, a@b.ru a@b.ru")
    assert contacts.telegram == ["@dev_ivan"]
    assert contacts.email == ["a@b.ru"]


def test_ignores_boilerplate_handles():
    assert extract_contacts("подписывайтесь на @channel").telegram == []


def test_no_contacts_yields_empty_result():
    contacts = extract_contacts("нужен сайт, детали в объявлении")
    assert not contacts.found
    assert contacts.lines() == []
    assert contacts.as_text() == ""
