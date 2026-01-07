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
        target = find_decision_maker(profile.name, client)
        
        if not target:
            frappe.db.set_value("Campaign Item", item_name, "status", "Data Missing")
            print(f"Skipping {profile.company_name}: No decision maker found.")
            return

        # Step 2: The Guesser
        email_guesses = generate_email_guesses(target['name'], profile.website)
        
        # Step 3: The Copywriter
        content = generate_copy(client, target, profile)
        
        if not content:
             frappe.db.set_value("Campaign Item", item_name, "status", "Generation Failed")
             print(f"Skipping {profile.company_name}: Content generation failed.")
             return

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

def find_decision_maker(profile_id, client):
    # Fetch all employees
    employees = frappe.get_all("Company Employee", 
        filters={"company_profile__id": profile_id},
        fields=["employee_name", "employee_title"]
    )
    
    if not employees:
        return None

    # Format employee list for the prompt
    employee_list_text = "\n".join([f"- {emp.employee_name} ({emp.employee_title})" for emp in employees if emp.employee_name])
    
    prompt = f"""
    You are an expert targeted outreach specialist.
    I need to find the best person to contact at this company for a sales pitch about automated lead generation services.
    
    Here is the list of employees:
    {employee_list_text}
    
    Rules:
    1. Prioritize roles like Founder, CEO, Owner, Managing Director, Sales Director, Marketing Director.
    2. If no perfect match, find the most senior person likely to handle growth or sales.
    3. Return valid JSON only with keys: "name", "title".
    4. If absolutely no suitable person is found in the list, return null.
    """

    try:
        from google.genai import types
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type='application/json'
            )
        )
        
        result = json.loads(response.text)
        
        # Handle list vs dict return
        if isinstance(result, list):
             if len(result) > 0:
                 result = result[0]
             else:
                 return None
                 
        if not result or not isinstance(result, dict):
            return None
            
        return {"name": result.get("name"), "title": result.get("title")}

    except Exception as e:
        print(f"Error in decision maker selection: {e}")
        # Fallback to first employee or None if critical failure, 
        # but let's just return None to be safe so we don't spam randoms.
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
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type='application/json'
            )
        )
        data = json.loads(response.text)
        
        # Fix for 'list' object has no attribute 'get'
        if isinstance(data, list):
            if len(data) > 0:
                data = data[0]
            else:
                return {"subject": "Generation Error", "body": "Empty list returned from AI"}
                
        if not isinstance(data, dict):
             return {"subject": "Generation Error", "body": f"Invalid format returned: {type(data)}"}
             
        return data
        
    except Exception as e:
        print(f"Gemini Error: {e}")
        return {"subject": "Error generating content", "body": str(e)}
