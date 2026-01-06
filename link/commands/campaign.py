import click
import frappe

@click.command("create-campaign-items")
@click.pass_context
@click.option("--campaign", required=True, help="Name of the Campaign")
def create_campaign_items(ctx, campaign):
    # Initialize Frappe site context
    if hasattr(ctx.obj, 'sites'):
        site = ctx.obj.sites[0]
    else:
        site = ctx.obj['sites'][0]
        
    frappe.init(site=site)
    frappe.connect()
    
    try:
        if not frappe.db.exists("Campaign", campaign):
            print(f"Campaign '{campaign}' not found.")
            return

        camp_doc = frappe.get_doc("Campaign", campaign)
        
        # Check validation status and mapping
        # Campaign.target_industry -> Profile.market_segment
        filters = {
            "market_segment": camp_doc.target_industry,
            "validation_status": "Verified"
        }
        
        if camp_doc.target_province:
            filters["province"] = camp_doc.target_province

        # Optimization: Prioritize web_enriched profiles
        # Sorting by web_enriched desc (1 first, then 0), then creation desc
        profiles = frappe.get_all("Profile", 
            filters=filters, 
            fields=["name", "company_name", "web_enriched", "slogan"],
            order_by="web_enriched desc, creation desc",
            limit=camp_doc.limit or 100
        )

        print(f"Found {len(profiles)} profiles matching criteria for campaign '{campaign}'.")

        count = 0
        for prof in profiles:
            # Avoid duplicates
            exists = frappe.db.exists("Campaign Item", {
                "campaign": campaign,
                "profile": prof.name
            })
            if exists:
                continue

            item = frappe.get_doc({
                "doctype": "Campaign Item",
                "campaign": campaign,
                "profile": prof.name,
                "status": "Pending",
                "enrichment_data": frappe.as_json({}) 
            })
            item.insert()
            
            count += 1
            if count % 10 == 0:
                frappe.db.commit()
                print(f"Created {count} items...")
        
        frappe.db.commit()
        print(f"Successfully created {count} Campaign Items for campaign '{campaign}'.")
        
    finally:
        frappe.destroy()
