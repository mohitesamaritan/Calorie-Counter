from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import google.generativeai as genai
from PIL import Image
import io
import json
import os
import requests
from typing import Optional
from supabase import create_client, Client

# ==========================================
# 1. SETUP & SECURE CONFIGURATION
# ==========================================
import os
from supabase import create_client, Client
from supabase.client import ClientOptions  # ✅ The correct class import

GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
SUPA_URL = os.environ.get("SUPABASE_URL")
SUPA_KEY = os.environ.get("SUPABASE_KEY")

genai.configure(api_key=GEMINI_KEY) 

# ✅ Pass the timeout settings using the dedicated ClientOptions object
custom_options = ClientOptions(
    postgrest_client_timeout=30,
    storage_client_timeout=30,
    schema="public"
)

supabase: Client = create_client(SUPA_URL, SUPA_KEY, options=custom_options)

model = genai.GenerativeModel('gemini-2.5-flash')

app = FastAPI(title="Calorie Counter - Mobile API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

security = HTTPBearer()

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    try:
        user_res = supabase.auth.get_user(token)
        if not user_res or not user_res.user: raise HTTPException(status_code=401, detail="Invalid token")
        res = supabase.table("users").select("*").eq("email", user_res.user.email).execute()
        if not res.data: raise HTTPException(status_code=404, detail="User profile not found")
        return res.data[0]
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Auth failed: {str(e)}")

# ==========================================
# 2. DATA MODELS & DYNAMIC MATH
# ==========================================
class AuthRequest(BaseModel): email: str; password: str
class ForgotPasswordRequest(BaseModel): email: str
class UserProfile(BaseModel):
    first_name: str; last_name: str; email: str; phone: str; password: str; age: int; gender: str
    height_cm: float; weight_kg: float; activity_level: str
    primary_goal: str; pace: str; diet_preference: str; alcohol_consumption: str
class MealLog(BaseModel): dish_name: str; calories: int; protein: int; carbs: int; fat: int; portion: float
class NotificationRegistration(BaseModel): notification_endpoint: str

def calculate_macros(profile: UserProfile):
    bmr = (10 * profile.weight_kg) + (6.25 * profile.height_cm) - (5 * profile.age) + (5 if profile.gender.lower() == "male" else -161)
    multipliers = {"sedentary": 1.2, "light": 1.375, "moderate": 1.55, "active": 1.725}
    tdee = bmr * multipliers.get(profile.activity_level.lower(), 1.2)
    
    # NEW GOAL LOGIC
    paces = {"slow": 250, "medium": 500, "fast": 750}
    adjustment = paces.get(profile.pace.lower(), 500)
    
    goal = profile.primary_goal.lower()
    if "lose" in goal: target_calories = max(tdee - adjustment, 1500 if profile.gender.lower() == "male" else 1200)
    elif "gain" in goal: target_calories = tdee + adjustment
    else: target_calories = tdee # Maintain

    base_water = int(profile.weight_kg * 35) 
    if profile.activity_level.lower() == 'moderate': base_water += 500
    elif profile.activity_level.lower() == 'active': base_water += 1000
    step_goals = {"sedentary": 5000, "light": 7500, "moderate": 10000, "active": 12000}

    return {
        "tdee": int(tdee), "target_calories": int(target_calories), "target_protein": int(profile.weight_kg * 2.0),
        "target_carbs": int((target_calories - ((profile.weight_kg * 2.0 * 4.0) + (target_calories * 0.25))) / 4.0),
        "target_fat": int((target_calories * 0.25) / 9.0), "diet_preference": profile.diet_preference,
        "target_water_ml": base_water, "target_steps": step_goals.get(profile.activity_level.lower(), 10000) 
    }

# ==========================================
# 3. SECURED API ENDPOINTS 
# ==========================================
@app.post("/forgot_password")
def forgot_password(req: ForgotPasswordRequest):
    try: supabase.auth.reset_password_for_email(req.email.strip())
    except Exception: pass 
    return {"status": "success"}

@app.post("/auth")
def authenticate(req: AuthRequest):
    try:
        auth_res = supabase.auth.sign_in_with_password({"email": req.email.strip(), "password": req.password})
        res = supabase.table("users").select("*").eq("email", req.email.strip()).execute()
        if res.data:
            user = res.data[0]
            activity = user.get("activity_level", "sedentary")
            base_water = int(user.get("weight_kg", 70) * 35) + (500 if activity == 'moderate' else 1000 if activity == 'active' else 0)
            macros = {
                "tdee": user["tdee"], "target_calories": user["target_calories"], "target_protein": user["target_protein"],
                "target_carbs": user["target_carbs"], "target_fat": user["target_fat"], "diet_preference": user["diet_preference"],
                "target_water_ml": base_water, "target_steps": {"sedentary": 5000, "light": 7500, "moderate": 10000, "active": 12000}.get(activity, 10000)
            }
            return {"status": "success", "user_id": user["id"], "goals": macros, "access_token": auth_res.session.access_token, "refresh_token": auth_res.session.refresh_token}
    except Exception: raise HTTPException(status_code=401, detail="Invalid Email/Password.")

@app.get("/me")
def get_me(user: dict = Depends(get_current_user)):
    activity = user.get("activity_level", "sedentary")
    base_water = int(user.get("weight_kg", 70) * 35) + (500 if activity == 'moderate' else 1000 if activity == 'active' else 0)
    macros = {
        "tdee": user["tdee"], "target_calories": user["target_calories"], "target_protein": user["target_protein"],
        "target_carbs": user["target_carbs"], "target_fat": user["target_fat"], "diet_preference": user["diet_preference"],
        "target_water_ml": base_water, "target_steps": {"sedentary": 5000, "light": 7500, "moderate": 10000, "active": 12000}.get(activity, 10000)
    }
    return {"status": "success", "user_id": user["id"], "goals": macros}

@app.post("/profile")
def create_profile(profile: UserProfile):
    try: 
        auth_res = supabase.auth.sign_up({"email": profile.email.strip(), "password": profile.password})
    except Exception as e: 
        # THIS IS THE NEW LINE: It will print the exact Supabase error to your Render logs!
        print(f"🔥 SUPABASE REJECTED REGISTRATION: {str(e)} 🔥")
        raise HTTPException(status_code=400, detail="Email already registered/invalid.")

    macros = calculate_macros(profile)
    user_data = {
        "first_name": profile.first_name.strip(), "last_name": profile.last_name.strip(), "email": profile.email.strip(), "phone": profile.phone.strip(),
        "age": profile.age, "gender": profile.gender, "height_cm": profile.height_cm, "weight_kg": profile.weight_kg, 
        "activity_level": profile.activity_level, "primary_goal": profile.primary_goal, "pace": profile.pace, 
        "diet_preference": profile.diet_preference, "alcohol_consumption": profile.alcohol_consumption,
        "tdee": macros['tdee'], "target_calories": macros['target_calories'], "target_protein": macros['target_protein'], "target_carbs": macros['target_carbs'], "target_fat": macros['target_fat']
    }
    res = supabase.table("users").insert(user_data).execute()
    return {"message": "Profile created", "user_id": res.data[0]['id'], "calculated_goals": macros, "access_token": auth_res.session.access_token if auth_res.session else None}
@app.post("/analyze")
async def analyze_food(file: Optional[UploadFile] = File(None), manual_dish: Optional[str] = Form(None), diet_preference: str = Form("Any"), current_user: dict = Depends(get_current_user)):
    try:
        diet_rules = f"\nCRITICAL: User diet is '{diet_preference}'. Flag alcohol with 'health_badge': 'Alcohol' and calculate exactly 1 Standard Serving."
        base_prompt = f"Return a JSON array of up to 4 most likely dish/drink options. Keys: dish_name, health_score, health_badge, estimated_calories, protein_grams, carbs_grams, fat_grams, healthier_swap_name. {diet_rules}"
        strict_config = genai.GenerationConfig(response_mime_type="application/json")

        if not file and manual_dish: response = model.generate_content(f"User ate: '{manual_dish}'. {base_prompt}", generation_config=strict_config)
        elif file and manual_dish: response = model.generate_content([f"User says '{manual_dish}'. {base_prompt}", Image.open(io.BytesIO(await file.read()))], generation_config=strict_config)
        elif file: response = model.generate_content([f"Identify this. {base_prompt}", Image.open(io.BytesIO(await file.read()))], generation_config=strict_config)
        else: raise HTTPException(status_code=400, detail="Provide image or text.")
        return json.loads(response.text)
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/log_meal")
def log_meal(meal: MealLog, current_user: dict = Depends(get_current_user)):
    supabase.table("meals").insert({"user_id": current_user["id"], "dish_name": meal.dish_name, "calories": meal.calories, "protein": meal.protein, "carbs": meal.carbs, "fat": meal.fat, "portion": meal.portion}).execute()
    return {"status": "success"}

@app.get("/close_day")
def close_day(current_user: dict = Depends(get_current_user)):
    meals_res = supabase.table("meals").select("*").eq("user_id", current_user["id"]).gte("created_at", "today").execute()
    if not meals_res.data: raise HTTPException(status_code=400, detail="No meals logged today!")
    
    tot_cal = sum(m['calories'] for m in meals_res.data)
    prompt = f"User closed day. Goal: {current_user['target_calories']} kcal. Actual: {tot_cal} kcal. Provide a VERY concise, punchy, encouraging 3-sentence summary of their day. No lists, no fluff. Just straight to the point."
    response = model.generate_content(prompt)
    
    # We now return the RAW data to the mobile app, so it can build the CSV natively!
    return {"report": response.text, "raw_meals": meals_res.data}

@app.get("/history")
def get_history(days: int = 7, current_user: dict = Depends(get_current_user)):
    meals_res = supabase.table("meals").select("*").eq("user_id", current_user["id"]).order("created_at", desc=False).execute()
    
    # Send both daily totals (for charts) and the raw meal history (for the Export feature)
    daily_totals = {}
    for meal in meals_res.data:
        date_only = meal['created_at'].split('T')[0]
        daily_totals[date_only] = daily_totals.get(date_only, 0) + meal['calories']
        
    sorted_dates = sorted(daily_totals.keys())[-days:]
    chart_data = [{"date": d[5:], "calories": daily_totals[d], "target": current_user['target_calories']} for d in sorted_dates]
    
    return {"chart_data": chart_data, "raw_history": meals_res.data}

@app.post("/register_notifications")
def register_notifications(payload: NotificationRegistration, current_user: dict = Depends(get_current_user)):
    supabase.table("users").update({"notification_endpoint": payload.notification_endpoint}).eq("id", current_user['id']).execute()
    return {"status": "success"}

@app.get("/scan_barcode/{barcode}")
def scan_barcode(barcode: str):
    try:
        data = requests.get(f"https://world.openfoodfacts.org/api/v2/product/{barcode}.json", headers={"User-Agent": "CalorieCounter/1.0"}).json()
        if data.get("status") == 1:
            n = data.get("product", {}).get("nutriments", {})
            return [{"dish_name": data["product"].get("product_name", "Unknown"), "health_score": 100, "health_badge": "Verified Barcode", "estimated_calories": int(n.get("energy-kcal_100g", 0)), "protein_grams": int(n.get("proteins_100g", 0)), "carbs_grams": int(n.get("carbohydrates_100g", 0)), "fat_grams": int(n.get("fat_100g", 0)), "healthier_swap_name": "N/A"}]
        raise HTTPException(status_code=404, detail="Barcode not found.")
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))