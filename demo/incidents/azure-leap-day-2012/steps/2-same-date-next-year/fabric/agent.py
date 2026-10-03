from fabric.certs import transfer_certificate_validity


def start_guest_agent() -> dict:
    valid_from, valid_to = transfer_certificate_validity()
    return {"valid_from": valid_from.isoformat(), "valid_to": valid_to.isoformat()}
