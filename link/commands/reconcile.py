import click
import frappe
from google.cloud import storage
import json
import os

@click.command("reconcile-profiles")
@click.pass_context
def reconcile_profiles(ctx):
    """
    Reconciles Profile records with GCS validation results.
    """
    # Initialize Frappe site if running from bench context
    site = ctx.obj['sites'][0]
    frappe.init(site=site)
    frappe.connect()

    try:
        reconcile_logic()
    finally:
        frappe.destroy()

def reconcile_logic():
    click.echo("Starting reconciliation process...")
    
    # Configuration
    BUCKET_NAME = "outreach-za-datalake"
    PREFIX = "data_lake/"
    
    # Initialize GCS Client
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(BUCKET_NAME)
        blobs = bucket.list_blobs(prefix=PREFIX)
    except Exception as e:
        click.echo(f"Error accessing GCS: {e}")
        return

    count = 0
    updated_count = 0
    
    # Iterate through GCS folders (implied by processing blobs)
    # Since list_blobs is flat, we need to group by folder or process metadata.json files directly
    
    # Strategy: Iterate only metadata.json files to identify records
    iterator = bucket.list_blobs(prefix=PREFIX)
    
    # We will track processed profiles to avoid duplicates if needed, 
    # but GCS structure implies one metadata.json per profile folder.
    
    for blob in iterator:
        if not blob.name.endswith("/metadata.json"):
            continue
            
        # Extract profile_id from path: data_lake/[profile_id]/metadata.json
        parts = blob.name.split('/')
        if len(parts) < 3:
            continue
            
        profile_id = parts[1] # "data_lake" is 0
        
        # Check if Profile exists in Frappe
        if not frappe.db.exists("Profile", profile_id):
            click.echo(f"Skipping {profile_id}: Not found in Frappe.")
            continue
            
        process_profile(bucket, profile_id, blob)
        
        count += 1
        updated_count += 1
        
        if updated_count % 100 == 0:
            frappe.db.commit()
            click.echo(f"Committed {updated_count} records...")

    frappe.db.commit()
    click.echo(f"Reconciliation complete. Processed {count} profiles.")

def process_profile(bucket, profile_id, metadata_blob):
    try:
        # Read metadata.json
        metadata_content = metadata_blob.download_as_text()
        metadata = json.loads(metadata_content)
        
        is_alive = metadata.get("is_alive", False)
        
        doc = frappe.get_doc("Profile", profile_id)
        dirty = False
        
        if is_alive:
            if doc.validation_status != "Verified":
                doc.validation_status = "Verified"
                dirty = True
            
            # Read content.md
            content_blob_path = f"data_lake/{profile_id}/content.md"
            content_blob = bucket.blob(content_blob_path)
            
            if content_blob.exists():
                content = content_blob.download_as_text()
                if doc.homepage_context != content:
                    doc.homepage_context = content
                    dirty = True
        else:
            if doc.validation_status != "Dead":
                doc.validation_status = "Dead"
                dirty = True
                
        if dirty:
            doc.save(ignore_permissions=True)
            # click.echo(f"Updated {profile_id}: {doc.validation_status}")
            
    except Exception as e:
        click.echo(f"Error processing {profile_id}: {e}")

