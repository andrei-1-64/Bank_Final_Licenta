"""Hardcoded "well-known subscription business" name/website list.

Lets `_detect_subscription_price_increase` (see payments/service.py)
recognize a recurring subscription payment from the RECIPIENT NAME the
sender typed, without requiring them to have first saved that IBAN as a
beneficiary and manually ticked "is a subscription" (see
beneficiaries/service.py). That manual flag is still honored too - this is
an additional, automatic trigger onto the SAME check, not a replacement,
because in practice almost nothing sets that flag: the standalone "add
beneficiary" form is the only place it's ever set true, so a payment made
straight from the Payments form or via the AI agent never qualifies on the
flag alone.

Matching is a case-insensitive substring test against the payer-supplied
beneficiary_name (PaymentCreate.beneficiary_name) - deliberately loose,
since real recipient names vary ("Netflix.com", "NETFLIX INTERNATIONAL
B.V.", "Netflix Inc"). A false positive here only means the price-increase
confirmation prompt shows up on what turns out to be a coincidentally-named
regular payment, never a security issue - just a UX heuristic, same
"good enough for a demo" bar as the rest of this feature (see
design_decisions).

Website is a best-effort "go here to cancel" link shown in the confirmation
prompt, used only when the beneficiary wasn't saved with its own website
(see payments/service.py) - a real user-supplied website always wins.
"""

KNOWN_SUBSCRIPTION_BUSINESSES: dict[str, str] = {
    "netflix": "https://www.netflix.com/cancelplan",
    "spotify": "https://www.spotify.com/account/subscription/",
    "disney plus": "https://www.disneyplus.com/account/subscription",
    "disney+": "https://www.disneyplus.com/account/subscription",
    "hbo max": "https://play.max.com/settings/subscription",
    "max.com": "https://play.max.com/settings/subscription",
    "amazon prime": "https://www.amazon.com/manageyourprime",
    "youtube premium": "https://www.youtube.com/paid_memberships",
    "youtube music": "https://www.youtube.com/paid_memberships",
    "apple music": "https://music.apple.com/account/subscriptions",
    "apple tv": "https://tv.apple.com/account/subscriptions",
    "apple one": "https://support.apple.com/en-us/109154",
    "icloud": "https://support.apple.com/en-us/109154",
    "google one": "https://one.google.com/about",
    "google play": "https://play.google.com/store/account/subscriptions",
    "playstation plus": "https://www.playstation.com/en-us/support/subscriptions/",
    "playstation network": "https://www.playstation.com/en-us/support/subscriptions/",
    "xbox game pass": "https://www.xbox.com/en-us/subscriptions/manage-subscriptions",
    "nintendo switch online": "https://www.nintendo.com/switch/online/",
    "ea play": "https://www.ea.com/ea-play",
    "ubisoft+": "https://www.ubisoft.com/en-us/ubisoft-plus",
    "crunchyroll": "https://www.crunchyroll.com/account/membership",
    "twitch": "https://www.twitch.tv/subscriptions",
    "audible": "https://www.audible.com/account/membership",
    "kindle unlimited": "https://www.amazon.com/kindle-dbs/hz/subscribe/ku",
    "adobe creative cloud": "https://account.adobe.com/plans",
    "adobe": "https://account.adobe.com/plans",
    "microsoft 365": "https://account.microsoft.com/services",
    "office 365": "https://account.microsoft.com/services",
    "dropbox": "https://www.dropbox.com/account/plan",
    "notion": "https://www.notion.so/my-integrations",
    "canva": "https://www.canva.com/settings/billing",
    "grammarly": "https://account.grammarly.com/subscription",
    "linkedin premium": "https://www.linkedin.com/premium/products/",
    "chatgpt": "https://chat.openai.com/#settings/Subscription",
    "openai": "https://platform.openai.com/account/billing",
    "midjourney": "https://www.midjourney.com/account/",
    "nordvpn": "https://my.nordaccount.com/billing/",
    "expressvpn": "https://www.expressvpn.com/subscriptions",
    "surfshark": "https://my.surfshark.com/subscription",
    "duolingo": "https://www.duolingo.com/settings/subscription",
    "headspace": "https://www.headspace.com/subscriptions/manage",
    "calm.com": "https://www.calm.com/account",
    "myfitnesspal": "https://www.myfitnesspal.com/account/subscription",
    "strava": "https://www.strava.com/subscribe/manage",
    "tinder": "https://www.help.tinder.com/hc/en-us/articles/115003765323",
    "bumble": "https://bumble.com/en/help/how-can-i-cancel-my-subscription",
    "zoom": "https://zoom.us/account/billing",
    "slack": "https://slack.com/help/articles/218915077",
    "github": "https://github.com/settings/billing",
    "norton": "https://www.norton.com/subscriptions",
    "mcafee": "https://www.mcafee.com/consumer/en-us/store/m0/account.html",
    "paramount+": "https://www.paramountplus.com/account/",
    "peacock": "https://www.peacocktv.com/account/subscription",
    "espn+": "https://www.espn.com/espnplus/subscription",
    "hulu": "https://secure.hulu.com/account",
    "dazn": "https://www.dazn.com/en-US/account",
    "patreon": "https://www.patreon.com/settings/memberships",
    "orange romania": "https://www.orange.ro/cont-orange",
    "vodafone romania": "https://www.vodafone.ro/myvodafone/",
    "digi romania": "https://www.digimobil.ro/myaccount",
    "telekom romania": "https://www.telekom.ro/myaccount",
    "rcs rds": "https://www.digi.ro/myaccount",
}

#: Longest name first, so a more specific match ("disney plus") is checked
#: before a shorter one that could also incidentally match.
_NAMES_BY_LENGTH_DESC = sorted(KNOWN_SUBSCRIPTION_BUSINESSES, key=len, reverse=True)


def match_known_subscription_business(beneficiary_name: str) -> str | None:
    """Best-effort cancel-URL for a known subscription business whose name
    appears (case-insensitive substring) in `beneficiary_name`, or None if
    it doesn't match any."""
    normalized = beneficiary_name.strip().lower()
    for name in _NAMES_BY_LENGTH_DESC:
        if name in normalized:
            return KNOWN_SUBSCRIPTION_BUSINESSES[name]
    return None
