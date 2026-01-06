import click
import frappe
import os
import json


@click.command("generate-campaign-content")
@click.pass_context
@click.option("--campaign", required=True, help="Name of the Campaign")
def generate_campaign_content(ctx, campaign):
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

        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            print("Error: GEMINI_API_KEY environment variable not set.")
            return

        try:
            from google import genai
            from google.genai import types
        except ImportError:
            print("Error: google-genai package is not installed. Please run `pip install google-genai`.")
            return

        client = genai.Client(api_key=api_key)

        # Fetch Pending items
        items = frappe.get_all("Campaign Item", 
            filters={
                "campaign": campaign,
                "status": "Pending"
            },
            fields=["name", "profile"]
        )
        
        print(f"Found {len(items)} pending items for campaign '{campaign}'.")
        
        count = 0
        for item_data in items:
            process_item(item_data['name'], item_data['profile'], client)
            count += 1
            if count % 10 == 0:
                frappe.db.commit()
                print(f"Processed {count} items...")

        frappe.db.commit()
        print(f"Finished processing. Total items: {count}")

    finally:
        frappe.destroy()

def process_item(item_name, profile_name, client):
    try:
        profile = frappe.get_doc("Profile", profile_name)
        
        # Step 1: The Headhunter
        target = find_decision_maker(profile.name)
        
        if not target:
            frappe.db.set_value("Campaign Item", item_name, "status", "Data Missing")
            print(f"Skipping {profile.company_name}: No decision maker found.")
            return

        # Step 2: The Guesser
        email_guesses = generate_email_guesses(target['name'], profile.website)
        
        # Step 3: The Copywriter
        content = generate_copy(client, target, profile)
        
        # Step 4: Save
        enrichment_data = {
            "email_guesses": email_guesses,
            "target_name": target['name'],
            "target_title": target['title']
        }
        
        frappe.db.set_value("Campaign Item", item_name, {
            "generated_subject": content.get("subject"),
            "generated_body": content.get("body"),
            "enrichment_data": json.dumps(enrichment_data),
            "status": "Drafted"
        })
        print(f"Drafted email for {target['name']} at {profile.company_name}")

    except Exception as e:
        print(f"Error processing {profile_name}: {e}")
        frappe.log_error(f"Generate Content Error: {e}")

def find_decision_maker(profile_id):
    # Priority order
    titles = ["Founder", "Owner", "Partner", "CEO", "Chief Executive Officer", "Managing Director", "Marketing Manager"]
    
    employees = frappe.get_all("Company Employee", 
        filters={"company_profile__id": profile_id},
        fields=["employee_name", "employee_title"]
    )
    
    if not employees:
        return None

    # Simple priority match
    for title_keyword in titles:
        for emp in employees:
            if not emp.employee_title:
                continue
            if title_keyword.lower() in emp.employee_title.lower():
                return {"name": emp.employee_name, "title": emp.employee_title}
                
    return None

def generate_email_guesses(name, website):
    if not website or not name:
        return []
        
    try:
        # Extract domain from url
        from urllib.parse import urlparse
        if not website.startswith(('http://', 'https://')):
            website = 'http://' + website
        domain = urlparse(website).netloc.replace('www.', '')
        
        name_parts = name.lower().split()
        if len(name_parts) < 2:
            return [f"{name_parts[0]}@{domain}"]
            
        first = name_parts[0]
        last = name_parts[-1]
        
        return [
            f"{first[0]}.{last}@{domain}", # f.last
            f"{first}@{domain}",          # first
            f"{first}.{last}@{domain}"    # first.last
        ]
    except Exception:
        return []

def generate_copy(client, target, profile):
    prompt = f"""
    You are a lead gen expert. 
    Target: {target['name']}, {target['title']} at {profile.company_name}.
    Website Context: {profile.homepage_context or 'No context available.'}
    
    Write a short, casual cold email (Subject + Body) pitching our automated outreach service. 
    Quote a specific fact from their website context to prove it's real.
    
    Return pure JSON with keys: "subject", "body".
    """
    
    try:
        from google.genai import types
        response = client.models.generate_content(
            model='gemini-2.0-flash-exp',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type='application/json'
            )
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"Gemini Error: {e}")
        return {"subject": "Error generating content", "body": str(e)}
