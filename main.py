from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
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
# 1. SETUP & CLOUD CONFIGURATION (SECURED)
# ==========================================
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
SUPA_URL = os.environ.get("SUPABASE_URL")
SUPA_KEY = os.environ.get("SUPABASE_KEY")

genai.configure(api_key=GEMINI_KEY) 
supabase: Client = create_client(SUPA_URL, SUPA_KEY)
model = genai.GenerativeModel('gemini-2.5-flash')

app = FastAPI(title="Calorie Counter - Secure Cloud Backend")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

security = HTTPBearer()

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    try:
        user_res = supabase.auth.get_user(token)
        if not user_res or not user_res.user:
            raise HTTPException(status_code=401, detail="Invalid token")
            
        email = user_res.user.email
        res = supabase.table("users").select("*").eq("email", email).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="User profile not found")
            
        return res.data[0]
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Authentication failed: {str(e)}")

# ==========================================
# 2. DATA MODELS & DYNAMIC MATH
# ==========================================
class AuthRequest(BaseModel):
    email: str
    password: str

class ForgotPasswordRequest(BaseModel):
    email: str

class UserProfile(BaseModel):
    first_name: str; last_name: str; email: str; phone: str; password: str; age: int; gender: str; height_cm: float; weight_kg: float; activity_level: str; deficit_tier: str; diet_preference: str; alcohol_consumption: str

class MealLog(BaseModel):
    dish_name: str; calories: int; protein: int; carbs: int; fat: int; portion: float

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

@app.post("/forgot_password")
def forgot_password(req: ForgotPasswordRequest):
    try:
        supabase.auth.reset_password_for_email(req.email.strip())
    except Exception:
        pass # Prevent user enumeration
    return {"status": "success", "message": "If this email is registered, a reset link has been sent."}

@app.post("/auth")
def authenticate(req: AuthRequest):
    try:
        auth_res = supabase.auth.sign_in_with_password({"email": req.email.strip(), "password": req.password})
        access_token = auth_res.session.access_token if auth_res.session else None
        refresh_token = auth_res.session.refresh_token if auth_res.session else None

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
            return {
                "status": "success", 
                "user_id": user["id"], 
                "goals": macros,
                "access_token": access_token,
                "refresh_token": refresh_token
            }
    except Exception as e:
        raise HTTPException(status_code=401, detail="Invalid Email or Password.")

@app.get("/me")
def get_me(current_user: dict = Depends(get_current_user)):
    user = current_user
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

@app.post("/profile")
def create_profile(profile: UserProfile):
    try:
        auth_res = supabase.auth.sign_up({"email": profile.email.strip(), "password": profile.password})
    except Exception as e:
        raise HTTPException(status_code=400, detail="Email already registered or invalid.")

    macros = calculate_macros(profile)
    
    user_data = {
        "first_name": profile.first_name.strip(), "last_name": profile.last_name.strip(), 
        "email": profile.email.strip(), "phone": profile.phone.strip(),
        "age": profile.age, "gender": profile.gender, 
        "height_cm": profile.height_cm, "weight_kg": profile.weight_kg, 
        "activity_level": profile.activity_level, "deficit_tier": profile.deficit_tier, 
        "diet_preference": profile.diet_preference, "alcohol_consumption": profile.alcohol_consumption,
        "tdee": macros['tdee'], "target_calories": macros['target_calories'], 
        "target_protein": macros['target_protein'], "target_carbs": macros['target_carbs'], "target_fat": macros['target_fat']
    }
    res = supabase.table("users").insert(user_data).execute()
    new_user_id = res.data[0]['id']
    
    access_token = auth_res.session.access_token if auth_res.session else None
    refresh_token = auth_res.session.refresh_token if auth_res.session else None
    
    return {
        "message": "Cloud Profile created", 
        "user_id": new_user_id, 
        "calculated_goals": macros,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "needs_verification": access_token is None
    }

@app.post("/analyze")
async def analyze_food(
    file: Optional[UploadFile] = File(None), 
    manual_dish: Optional[str] = Form(None), 
    diet_preference: str = Form("Any"), 
    alcohol: str = Form("None"),
    current_user: dict = Depends(get_current_user)
):
    try:
        # ADVANCED ALCOHOL LOGIC
        diet_rules = f"\nCRITICAL: User diet is '{diet_preference}'. If the image or text is an alcoholic beverage (bottle, can, poured drink), you MUST set 'health_badge' to 'Alcohol'. You MUST identify the specific type (e.g., 'Scotch Whisky', 'Wheat Beer'). You MUST calculate calories and macros for exactly ONE STANDARD SERVING (e.g., 1 Peg/30ml for spirits, 1 Pint/330ml for beer). Set the dish_name to '[Brand/Type] (1 Peg/Beer)' so the user can multiply it."
        
        base_prompt = f"Return a JSON array of up to 4 most likely dish/drink options. Each object must contain exactly these keys: dish_name (string), health_score (integer), health_badge (string), estimated_calories (integer), protein_grams (integer), carbs_grams (integer), fat_grams (integer), healthier_swap_name (string). {diet_rules}"

        strict_config = genai.GenerationConfig(response_mime_type="application/json")

        if not file and manual_dish: 
            response = model.generate_content(f"User ate/drank: '{manual_dish}'. {base_prompt}", generation_config=strict_config)
        elif file and manual_dish: 
            response = model.generate_content([f"User says this is '{manual_dish}'. Use image for portion. {base_prompt}", Image.open(io.BytesIO(await file.read()))], generation_config=strict_config)
        elif file and not manual_dish: 
            response = model.generate_content([f"Identify this food or drink. {base_prompt}", Image.open(io.BytesIO(await file.read()))], generation_config=strict_config)
        else: 
            raise HTTPException(status_code=400, detail="Must provide image or text.")
        
        try:
            return json.loads(response.text)
        except Exception as parse_error:
            raise HTTPException(status_code=500, detail=f"AI Format Error: {parse_error}. Raw: {response.text}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/log_meal")
def log_meal(meal: MealLog, current_user: dict = Depends(get_current_user)):
    meal_data = {
        "user_id": current_user["id"], "dish_name": meal.dish_name, "calories": meal.calories, 
        "protein": meal.protein, "carbs": meal.carbs, "fat": meal.fat, "portion": meal.portion
    }
    supabase.table("meals").insert(meal_data).execute()
    return {"status": "success"}

@app.get("/close_day")
def close_day(current_user: dict = Depends(get_current_user)):
    user = current_user
    user_id = user["id"]
    
    meals_res = supabase.table("meals").select("*").eq("user_id", user_id).gte("created_at", "today").execute()
    meals = meals_res.data
    if not meals: raise HTTPException(status_code=400, detail="No meals logged today!")

    filepath = os.path.join(os.getcwd(), f"dietitian_report_{user['first_name']}.csv")
    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(["Daily Dietitian Report", f"Client: {user['first_name']} {user['last_name']}", f"Diet: {user['diet_preference']}"])
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

    prompt = f"You are an expert fitness coach. User just closed day. TDEE is {user['tdee']} kcal. Goal is {user['target_calories']} kcal. Actual today: {tot_cal} kcal. Provide: 1. Encouraging Summary. 2. Clinical Breakdown. 3. Action Plan."
    response = model.generate_content(prompt)
    return {"report": response.text, "file_path": filepath}

@app.get("/history")
def get_history(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    target = current_user['target_calories']
    
    meals_res = supabase.table("meals").select("created_at, calories").eq("user_id", user_id).execute()
    daily_totals = {}
    for meal in meals_res.data:
        date_only = meal['created_at'].split('T')[0]
        if date_only not in daily_totals: daily_totals[date_only] = 0
        daily_totals[date_only] += meal['calories']
        
    sorted_dates = sorted(daily_totals.keys())[-7:]
    chart_data = [{"date": d[5:], "calories": daily_totals[d], "target": target} for d in sorted_dates]
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
            raise HTTPException(status_code=404, detail="Barcode not found.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))