"""Outlook deep links must open in the mailbox the message was synced from.

Graph's webLink carries no mailbox, so it resolves against whoever is signed in
— every link rendered "This message might have been moved or deleted" for
anyone but that mailbox's owner (seen 2026-09-09 on an ir@ message).
"""
from dashboard.data import mailbox_deep_link

BARE = ("https://outlook.office365.com/owa/?ItemID=AAMkADUx%2BNi&exvsurl=1"
        "&viewmodel=ReadMessageItem")
SCOPED = ("https://outlook.office365.com/owa/ir@exigentcap.com/?ItemID=AAMkADUx%2BNi"
          "&exvsurl=1&viewmodel=ReadMessageItem")


def test_stamps_the_arrival_mailbox_into_the_path():
    assert mailbox_deep_link(BARE, "ir@exigentcap.com") == SCOPED


def test_percent_encoded_itemid_survives_untouched():
    assert "ItemID=AAMkADUx%2BNi" in mailbox_deep_link(BARE, "ir@exigentcap.com")


def test_already_scoped_link_is_not_double_stamped():
    assert mailbox_deep_link(SCOPED, "ir@exigentcap.com") == SCOPED


def test_missing_inputs_degrade_to_the_original():
    assert mailbox_deep_link(BARE, "") == BARE
    assert mailbox_deep_link(BARE, None) == BARE
    assert mailbox_deep_link("", "ir@exigentcap.com") == ""
    assert mailbox_deep_link(None, "ir@exigentcap.com") == ""


def test_a_mailbox_that_is_not_a_plain_address_is_refused():
    for bad in ("../../evil", "ir@exigentcap.com/x?y", "notanemail", "a b@c"):
        assert mailbox_deep_link(BARE, bad) == BARE


def test_non_owa_links_are_left_alone():
    other = "https://teams.microsoft.com/l/message/19:abc/1234"
    assert mailbox_deep_link(other, "ir@exigentcap.com") == other


def test_outlook_office_com_without_365():
    o = "https://outlook.office.com/owa/?ItemID=XYZ"
    assert mailbox_deep_link(o, "ir@exigentcap.com") ==         "https://outlook.office.com/owa/ir@exigentcap.com/?ItemID=XYZ"

def test_ingestion_stamps_the_mailbox_it_read_from():
    """The authoritative fix: sync writes the scoped link, so it never depends on
    provenance (create-time mailbox) matching sourcelink (last-resync mailbox)."""
    from ingestion.sync import mailbox_deep_link as ingest_link
    assert ingest_link(BARE, "ir@exigentcap.com") == SCOPED
    assert ingest_link(SCOPED, "ebrender@exigentcap.com") == SCOPED  # no re-stamp
    assert ingest_link(BARE, "") == BARE
