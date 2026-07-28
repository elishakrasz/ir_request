from ingestion.matching import classify_direction

ORG = ["exigentcap.com"]


def test_external_sender_is_inbound():
    assert classify_direction("anna@lpfund.com", ["ir@exigentcap.com"], ORG) == "Inbound"


def test_internal_to_external_is_outbound():
    assert classify_direction("ir@exigentcap.com", ["anna@lpfund.com"], ORG) == "Outbound"


def test_internal_to_internal_is_internal():
    # a colleague's mail in a synced Inbox must NEVER register as Outbound
    assert classify_direction("edavis@exigentcap.com", ["ir@exigentcap.com"], ORG) == "Internal"


def test_bcc_only_external_is_outbound():
    # bccRecipients are part of the recipient set (SentItems edge)
    assert classify_direction("ir@exigentcap.com",
                              ["edavis@exigentcap.com", "anna@lpfund.com"], ORG) == "Outbound"


def test_mixed_case_domains():
    assert classify_direction("IR@ExigentCap.com", ["Anna@LPFund.com"], ORG) == "Outbound"
