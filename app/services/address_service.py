import json

FIELDS = ("name", "phone", "address1", "address2", "city", "province", "zip", "country")


def normalize_address(value):
    result = {key: str(value.get(key) or "").strip()[:255] for key in FIELDS}
    if not result["name"]:
        result["name"] = " ".join(str(value.get(k) or "").strip() for k in ("first_name", "last_name")).strip()
    return result


def pickup_from_form(form, order):
    try:
        value = json.loads(form["pickup_address"]) if "pickup_address" in form else order.shipping_address
    except (ValueError, TypeError):
        raise ValueError("Please enter a valid pickup address")
    if not isinstance(value, dict):
        raise ValueError("Please enter your pickup address")
    if any(not isinstance(v, str) or len(v) > 255 for k, v in value.items() if k in FIELDS):
        raise ValueError("Address fields must be text of at most 255 characters")
    address = normalize_address(value)
    if any(not address[k] for k in FIELDS if k != "address2"):
        raise ValueError("Complete all pickup address fields except address line 2")
    if not 7 <= sum(c.isdigit() for c in address["phone"]) <= 15:
        raise ValueError("Enter a valid pickup phone number")
    return address
