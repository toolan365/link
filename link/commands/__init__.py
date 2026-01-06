from .reconcile import reconcile_profiles
from .enrich import enrich_profiles
from .campaign import create_campaign_items

commands = [
	reconcile_profiles,
	enrich_profiles,
	create_campaign_items
]