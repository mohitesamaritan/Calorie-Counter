from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import google.generativeai as genai
from PIL import Image
import io
import json
import csv
import os
import requests
from typing import Optional
from supabase import create_client, Client

# ==========================================
# 1. SETUP & CLOUD CONFIGURATION
# ==========================================
genai.configure(api_key="AIzaSyD0FJXNr3JwardPp3pRJhQY_2SVZdiPvaw") 

SUPABASE_URL = "https://dnfgurbtamrcqyxuzdxv.supabase.co"     
SUPABASE_KEY = "sb_publishable_wsKvQ91uxeT-HXGBsa-icw_QI98bliJ"         
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

model = genai.GenerativeModel('gemini-2.5-flash')

app = FastAPI(title="Calorie Counter - Secure Cloud Backend")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# ==========================================
# 2. DATA MODELS & DYNAMIC MATH
# ==========================================
# FIXED: Login now requires Email and Password
class AuthRequest(BaseModel):
    email: str
    password: str

# FIXED: Profile creation now requires a Password
class UserProfile(BaseModel):
    name: str; email: str; phone: str; password: str; age: int; gender: str; height_cm: float; weight_kg: float; activity_level: str; deficit_tier: str; diet_preference: str

class MealLog(BaseModel):
    user_id: int; dish_name: str; calories: int; protein: int; carbs: int; fat: int; portion: float

def calculate_macros(profile: UserProfile):
    bmr = (10 * profile.weight_kg) + (6.25 * profile.height_cm) - (5 * profile.age) + (5 if profile.gender.lower() == "male" else -161)
    multipliers = {"sedentary": 1.2, "light": 1.375, "moderate": 1.55, "active": 1.725}
    tdee = bmr * multipliers.get(profile.activity_level.lower(), 1.2)
    deficits = {"low": 250, "medium": 500, "high": 750, "aggressive": 1000}
    target_calories = max(tdee - deficits.get(profile.deficit_tier.lower(), 500), 1500 if profile.gender.lower() == "male" else 1200)

    base_water = int(profile.weight_kg * 35) 
    if profile.activity_level.lower() == 'moderate': base_water += 500
    elif profile.activity_level.lower() == 'active': base_water += 1000
    
    step_goals = {"sedentary": 5000, "light": 7500, "moderate": 10000, "active": 12000}
    target_steps = step_goals.get(profile.activity_level.lower(), 10000)

    return {
        "tdee": int(tdee), "target_calories": int(target_calories), "target_protein": int(profile.weight_kg * 2.0),
        "target_carbs": int((target_calories - ((profile.weight_kg * 2.0 * 4.0) + (target_calories * 0.25))) / 4.0),
        "target_fat": int((target_calories * 0.25) / 9.0), "diet_preference": profile.diet_preference,
        "target_water_ml": base_water, "target_steps": target_steps 
    }

# ==========================================
# 3. API ENDPOINTS (SECURED)
# ==========================================

@app.post("/auth")
def authenticate(req: AuthRequest):
    try:
        # SECURE: Validates password cryptographically against Supabase Auth
        supabase.auth.sign_in_with_password({"email": req.email.strip(), "password": req.password})
        
        # If password is correct, grab their data
        res = supabase.table("users").select("*").eq("email", req.email.strip()).execute()
        if res.data:
            user = res.data[0]
            weight = user.get("weight_kg", 70)
            activity = user.get("activity_level", "sedentary")
            base_water = int(weight * 35)
            if activity == 'moderate': base_water += 500
            elif activity == 'active': base_water += 1000
            step_goals = {"sedentary": 5000, "light": 7500, "moderate": 10000, "active": 12000}

            macros = {
                "tdee": user["tdee"], "target_calories": user["target_calories"],
                "target_protein": user["target_protein"], "target_carbs": user["target_carbs"],
                "target_fat": user["target_fat"], "diet_preference": user["diet_preference"],
                "target_water_ml": base_water, "target_steps": step_goals.get(activity, 10000)
            }
            return {"status": "success", "user_id": user["id"], "goals": macros}
    except Exception as e:
        raise HTTPException(status_code=401, detail="Invalid Email or Password.")

@app.post("/profile")
def create_profile(profile: UserProfile):
    try:
        # SECURE: Registers the user cryptographically in Supabase Auth
        supabase.auth.sign_up({"email": profile.email.strip(), "password": profile.password})
    except Exception as e:
        raise HTTPException(status_code=400, detail="Email already registered or invalid.")

    macros = calculate_macros(profile)
    
    user_data = {
        "name": profile.name.strip(), "email": profile.email.strip(), "phone": profile.phone.strip(),
        "age": profile.age, "gender": profile.gender, 
        "height_cm": profile.height_cm, "weight_kg": profile.weight_kg, 
        "activity_level": profile.activity_level, "deficit_tier": profile.deficit_tier, 
        "diet_preference": profile.diet_preference, "tdee": macros['tdee'], 
        "target_calories": macros['target_calories'], "target_protein": macros['target_protein'], 
        "target_carbs": macros['target_carbs'], "target_fat": macros['target_fat']
    }
    res = supabase.table("users").insert(user_data).execute()
    new_user_id = res.data[0]['id']
    
    return {"message": "Cloud Profile created", "user_id": new_user_id, "calculated_goals": macros}

@app.post("/analyze")
async def analyze_food(file: Optional[UploadFile] = File(None), manual_dish: Optional[str] = Form(None), diet_preference: str = Form("Any")):
    try:
        diet_rules = f"\nCRITICAL: The user's diet is '{diet_preference}'. Swap suggestions MUST adhere strictly to a {diet_preference} diet."
        base_prompt = f"Return a JSON array of up to 4 most likely dish options. Each object must contain: dish_name, health_score, health_badge, estimated_calories, protein_grams, carbs_grams, fat_grams, healthier_swap_name. {diet_rules} Return ONLY raw JSON."

        if not file and manual_dish: response = model.generate_content(f"User ate: '{manual_dish}'. {base_prompt}")
        elif file and manual_dish: response = model.generate_content([f"User says this is '{manual_dish}'. Use image for portion/oil. {base_prompt}", Image.open(io.BytesIO(await file.read()))])
        elif file and not manual_dish: response = model.generate_content([f"Identify this Indian food. {base_prompt}", Image.open(io.BytesIO(await file.read()))])
        else: raise HTTPException(status_code=400, detail="Must provide image or text.")
        
        try:
            clean_text = response.text.replace("```json", "").replace("```", "").strip()
            return json.loads(clean_text)
        except Exception as parse_error:
            raise HTTPException(status_code=500, detail=f"AI Format Error: {parse_error}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/log_meal")
def log_meal(meal: MealLog):
    meal_data = {
        "user_id": meal.user_id, "dish_name": meal.dish_name, "calories": meal.calories, 
        "protein": meal.protein, "carbs": meal.carbs, "fat": meal.fat, "portion": meal.portion
    }
    supabase.table("meals").insert(meal_data).execute()
    return {"status": "success"}

@app.get("/close_day")
def close_day(user_id: int):
    user_res = supabase.table("users").select("*").eq("id", user_id).execute()
    if not user_res.data: raise HTTPException(status_code=400, detail="No user found.")
    user = user_res.data[0]
    
    meals_res = supabase.table("meals").select("*").eq("user_id", user_id).gte("created_at", "today").execute()
    meals = meals_res.data

    if not meals:
        raise HTTPException(status_code=400, detail="No meals logged today!")

    filepath = os.path.join(os.getcwd(), f"dietitian_report_{user['name']}.csv")
    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(["Daily Dietitian Report", f"Client: {user['name']}", f"Diet: {user['diet_preference']}"])
        writer.writerow(["Target Calories:", user['target_calories'], "TDEE (Maintenance):", user['tdee']])
        writer.writerow([])
        writer.writerow(["Dish Name", "Portions", "Calories", "Protein (g)", "Carbs (g)", "Fat (g)", "Time Logged"])
        
        tot_cal = tot_pro = tot_carb = tot_fat = 0
        for m in meals:
            time_logged = m['created_at'].split("T")[1][:5] if 'T' in m['created_at'] else "N/A"
            writer.writerow([m['dish_name'], m['portion'], m['calories'], m['protein'], m['carbs'], m['fat'], time_logged])
            tot_cal += m['calories']; tot_pro += m['protein']; tot_carb += m['carbs']; tot_fat += m['fat']
            
        writer.writerow([])
        writer.writerow(["TOTAL CONSUMED", "-", tot_cal, tot_pro, tot_carb, tot_fat, "-"])
        writer.writerow(["TARGET MACROS", "-", user['target_calories'], user['target_protein'], user['target_carbs'], user['target_fat'], "-"])

    prompt = f"""
    You are an expert fitness coach. The user just closed their day.
    User Profile: TDEE is {user['tdee']} kcal. Goal is {user['target_calories']} kcal.
    Actual today: {tot_cal} kcal, {tot_pro}g protein, {tot_carb}g carbs, {tot_fat}g fat.
    Provide: 1. Encouraging Summary. 2. Clinical Breakdown. 3. Action Plan (Specific activity needed tomorrow if they went over {user['tdee']} kcal). Format as plain text.
    """
    
    response = model.generate_content(prompt)
    return {"report": response.text, "file_path": filepath}

@app.get("/history")
def get_history(user_id: int):
    user_res = supabase.table("users").select("target_calories").eq("id", user_id).execute()
    if not user_res.data: raise HTTPException(status_code=400, detail="User not found")
    target = user_res.data[0]['target_calories']
    
    meals_res = supabase.table("meals").select("created_at, calories").eq("user_id", user_id).execute()
    
    daily_totals = {}
    for meal in meals_res.data:
        date_only = meal['created_at'].split('T')[0]
        if date_only not in daily_totals:
            daily_totals[date_only] = 0
        daily_totals[date_only] += meal['calories']
        
    sorted_dates = sorted(daily_totals.keys())[-7:]
    
    chart_data = []
    for d in sorted_dates:
        chart_data.append({
            "date": d[5:], 
            "calories": daily_totals[d],
            "target": target
        })
        
    return {"chart_data": chart_data}

@app.get("/scan_barcode/{barcode}")
def scan_barcode(barcode: str):
    try:
        url = f"https://world.openfoodfacts.org/api/v2/product/{barcode}.json"
        response = requests.get(url, headers={"User-Agent": "CalorieCounter/1.0"})
        data = response.json()

        if data.get("status") == 1:
            product = data.get("product", {})
            nutriments = product.get("nutriments", {})

            name = product.get("product_name", "Unknown Packaged Item")
            cals = nutriments.get("energy-kcal_100g", 0)
            pro = nutriments.get("proteins_100g", 0)
            carb = nutriments.get("carbohydrates_100g", 0)
            fat = nutriments.get("fat_100g", 0)

            return [{
                "dish_name": f"{name} (per 100g)",
                "health_score": 100, 
                "health_badge": "Verified Barcode",
                "estimated_calories": int(cals),
                "protein_grams": int(pro),
                "carbs_grams": int(carb),
                "fat_grams": int(fat),
                "healthier_swap_name": "N/A"
            }]
        else:
            raise HTTPException(status_code=404, detail="Barcode not found in OpenFoodFacts database.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))