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
    if hasattr(ctx.obj, 'sites'):
        site = ctx.obj.sites[0]
    else:
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
    PROJECT_ID = "link-za"
    BUCKET_NAME = "outreach-za-datalake"
    # User clarification: Bucket has no subdirectory, structure is bucket/profile_id/metadata.json
    PREFIX = "" 
    
    # Initialize GCS Client
    try:
        storage_client = storage.Client(project=PROJECT_ID)
        bucket = storage_client.bucket(BUCKET_NAME)
        # Note: list_blobs with prefix="" lists everything
        iterator = bucket.list_blobs(prefix=PREFIX)
    except Exception as e:
        click.echo(f"Error accessing GCS: {e}")
        return

    count = 0
    updated_count = 0
    
    click.echo(f"Scanning bucket {BUCKET_NAME}...")

    # Iterate through GCS blobs
    for blob in iterator:
        # We only care about metadata.json files to identify a profile folder
        if not blob.name.endswith("/metadata.json"):
            continue
            
        # Expected structure: [profile_id]/metadata.json
        parts = blob.name.split('/')
        
        # If structure was data_lake/id/meta, len was 3. 
        # Now if it is id/meta, len is 2.
        if len(parts) < 2: 
            continue
            
        # profile_id is the folder name, which is the second to last part
        # e.g. "my-profile-id/metadata.json" -> parts[0] is id
        # This works even if there is a prefix, parts[-2] is always the parent folder
        profile_id = parts[-2]
        
        # Check if Profile exists in Frappe
        if not frappe.db.exists("Profile", profile_id):
            # click.echo(f"Skipping {profile_id}: Not found in Frappe.")
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
            # Structure: [profile_id]/content.md
            content_blob_path = f"{profile_id}/content.md"
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

