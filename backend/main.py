from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import requests
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ChatRequest(BaseModel):
    message: str

@app.get("/")
def home():
    return {"status": "AI Robot Assistant Running 🚀"}

@app.post("/chat")
def chat(data: ChatRequest):
    user_message = data.message
    print(f"User Said: {user_message}")
    
    # ================== GROQ API (Text Response) ==================
    groq_key = os.getenv("GROQ_API_KEY")
    if not groq_key:
        return {"response": "Groq API key nahi mili. .env check karo."}

    url = "https://api.groq.com/openai/v1/chat/completions"
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": user_message}],
        "temperature": 0.7,
        "max_tokens": 800
    }
    headers = {
        "Authorization": f"Bearer {groq_key}",
        "Content-Type": "application/json"
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=20)
        res_json = response.json()
        
        if response.status_code == 200:
            bot_response = res_json['choices'][0]['message']['content']
            print("✅ Groq Success")
        else:
            print("Groq Error:", res_json)
            bot_response = "Abhi thoda busy hun, dobara try karo."
    except Exception as e:
        print("Groq Error:", str(e))
        bot_response = "Server mein issue hai. Thodi der baad try karo."
    
    # ================== RETURN RESPONSE ==================
    return {"response": bot_response}