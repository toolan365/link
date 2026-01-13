import click
import frappe
from google.cloud import storage
import json
import logging
import re
import trafilatura
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from typing import Set, Dict
from datetime import datetime
# Logging setup
logger = logging.getLogger(__name__)
@click.command("enrich-profiles")
@click.pass_context
def enrich_profiles(ctx):
    """
    Enriches 'Verified' Profile records by scraping their websites and saving data to GCS.
    """
    if hasattr(ctx.obj, 'sites'):
        site = ctx.obj.sites[0]
    else:
        site = ctx.obj['sites'][0]
        
    frappe.init(site=site)
    frappe.connect()
    try:
        enrichment_logic()
    finally:
        frappe.destroy()
def enrichment_logic():
    click.echo("Starting profile enrichment...")
    
    # Configuration
    PROJECT_ID = "link-za"
    BUCKET_NAME = "outreach-za-datalake"
    
    # Initialize GCS
    try:
        storage_client = storage.Client(project=PROJECT_ID)
        bucket = storage_client.bucket(BUCKET_NAME)
    except Exception as e:
        click.echo(f"Error accessing GCS: {e}")
        return
    # Fetch verified profiles that are either not enriched or enriched > 7 days ago (stale)
    # User specified "web_enriched_date > 7 days ago", interpreted as "Age > 7 days" i.e. Date < (Now - 7 days)
    # Also web_enriched_date is a Date field.
    profiles = frappe.db.sql("""
        SELECT name, profile_id, website 
        FROM `tabProfile` 
        WHERE validation_status = 'Verified'
        AND (
            web_enriched = 0 
            OR web_enriched IS NULL 
            OR web_enriched_date < DATE_SUB(CURDATE(), INTERVAL 1 MONTH)
        )
    """, as_dict=True)
    click.echo(f"Found {len(profiles)} profiles to enrich.")
    for p in profiles:
        profile_id = p.profile_id
        website = p.website
        
        if not website:
            click.echo(f"Skipping {profile_id}: No website.")
            continue
        try:
            click.echo(f"Processing {profile_id} ({website})...")
            # Increased pages to 7 as requested
            harvester = GCSSiteHarvester(profile_id, website, bucket)
            harvester.run(max_internal_pages=7)
            
            # Update Profile
            frappe.db.set_value("Profile", p.name, "web_enriched", 1)
            # Use today() for Date field
            frappe.db.set_value("Profile", p.name, "web_enriched_date", frappe.utils.today())
            frappe.db.commit()
            
        except Exception as e:
            click.echo(f"Failed to enrich {profile_id}: {e}")
            frappe.db.rollback()
    click.echo("Enrichment complete.")
class GCSSiteHarvester:
    def __init__(self, company_id: str, base_url: str, bucket):
        self.company_id = company_id
        self.base_url = base_url.rstrip("/")
        self.domain = urlparse(self.base_url).netloc
        self.bucket = bucket
        
        # GCS Prefix: [profile_id]/
        self.prefix = f"{company_id}/"
        
        self.internal_links: Set[str] = set()
        self.external_links: Set[str] = set()
        self.contacts: Dict[str, Set[str]] = {
            "emails": set(),
            "phones": set(),
            "socials": set()
        }
    def is_internal(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.netloc == '':
            return True
            
        base_domain = self.domain.replace('www.', '')
        target_domain = parsed.netloc.replace('www.', '')
        return base_domain == target_domain
    def clean_url(self, url: str) -> str:
        full_url = urljoin(self.base_url, url)
        return full_url.split("#")[0].rstrip("/")
    def extract_links_and_contacts(self, html: str, source_url: str):
        if not html: return
        soup = BeautifulSoup(html, 'html.parser')
        
        for a in soup.find_all('a', href=True):
            href = a['href'].strip()
            
            if href.startswith('mailto:'):
                email = href.replace('mailto:', '').split('?')[0]
                if email: self.contacts["emails"].add(email)
                continue
            
            if href.startswith('tel:'):
                phone = href.replace('tel:', '').split('?')[0]
                if phone: self.contacts["phones"].add(phone)
                continue
            if not href or href.startswith(('#', 'javascript:', 'data:')):
                continue
                
            full_url = self.clean_url(href)
            
            if self.is_internal(full_url):
                if full_url != self.base_url and full_url.startswith('http'):
                    self.internal_links.add(full_url)
            else:
                if full_url.startswith('http'):
                    self.external_links.add(full_url)
    def harvest_socials_from_externals(self):
        social_patterns = [
            r'linkedin\.com', r'facebook\.com', r'twitter\.com', 
            r'x\.com', r'instagram\.com', r'youtube\.com'
        ]
        for url in list(self.external_links):
            if any(re.search(pattern, url, re.I) for pattern in social_patterns):
                self.contacts["socials"].add(url)
    def fetch_and_save_page(self, url: str):
        try:
            downloaded = trafilatura.fetch_url(url)
            if downloaded:
                self.extract_links_and_contacts(downloaded, url)
                content = trafilatura.extract(downloaded)
                
                if content:
                    slug = urlparse(url).path.strip("/").replace("/", "_") or "index_alt"
                    # GCS Path: [profile_id]/pages/[slug].md
                    blob_path = f"{self.prefix}pages/{slug}.md"
                    
                    blob = self.bucket.blob(blob_path)
                    blob.upload_from_string(content, content_type="text/markdown")
        except Exception as e:
            error_str = str(e)
            if "Provided scope(s) are not authorized" in error_str:
                logger.error(f"GCS SCOPE ERROR: {url}. The VM's Access Scopes likely block writing. Stop VM -> Edit -> Access Scopes -> Allow credentials to write to Storage (or 'Allow full access').")
            else:
                logger.error(f"Failed to harvest page {url}: {e}")
    def run(self, max_internal_pages: int = 5):
        # 1. Fetch Homepage
        try:
            html = trafilatura.fetch_url(self.base_url)
            if not html:
                logger.warning(f"Could not fetch homepage for {self.base_url}")
                return
                
            # Save raw homepage HTML (optional but good for consistency/debugging)
            raw_blob = self.bucket.blob(f"{self.prefix}raw.html")
            raw_blob.upload_from_string(html, content_type="text/html")
            
        except Exception as e:
            error_str = str(e)
            if "Provided scope(s) are not authorized" in error_str:
                logger.error(f"GCS SCOPE ERROR: {self.base_url}. The VM's Access Scopes deny write access. Please enable 'Allow full access to all Cloud APIs' on the VM instance.")
                # If scope is wrong, no point continuing for this record
                raise e 
            
            logger.error(f"Error fetching homepage {self.base_url}: {e}")
            return
        # 2. Initial Extraction
        self.extract_links_and_contacts(html, self.base_url)
        self.harvest_socials_from_externals()
        
        # 3. Follow limited internal pages
        keyword_scores = {
            'contact': 10, 'about': 5, 'team': 5,
            'profile': 5, 'services': 3, 'career': 3
        }
        
        def get_score(url):
            url_lower = url.lower()
            score = 0
            for kw, val in keyword_scores.items():
                if kw in url_lower: score += val
            return score
        
        sorted_internals = sorted(list(self.internal_links), key=get_score, reverse=True)
        to_fetch = sorted_internals[:max_internal_pages]
        
        for url in to_fetch:
            self.fetch_and_save_page(url)
            
        # 4. Final Social Harvest
        self.harvest_socials_from_externals()
        
        # 5. Save Results
        self.save_results()
    def save_results(self):
        try:
            # Contacts
            contacts_data = {
                "emails": sorted(list(self.contacts["emails"])),
                "phones": sorted(list(self.contacts["phones"])),
                "socials": sorted(list(self.contacts["socials"]))
            }
            
            contacts_blob = self.bucket.blob(f"{self.prefix}contacts.json")
            contacts_blob.upload_from_string(
                json.dumps(contacts_data, indent=2), 
                content_type="application/json"
            )
            # External Links
            external_content = "\n".join(sorted(self.external_links))
            ext_blob = self.bucket.blob(f"{self.prefix}external.txt")
            ext_blob.upload_from_string(external_content, content_type="text/plain")
        except Exception as e:
             logger.error(f"Failed to save results for {self.company_id}: {e}")
