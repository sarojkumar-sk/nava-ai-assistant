const chatBox = document.getElementById("chatBox");
const userInput = document.getElementById("userInput");
//const robot = document.getElementById("robot");

function addMessage(text, sender) {
  const div = document.createElement("div");
  div.className = `message ${sender}-message`;
  div.textContent = text;
  chatBox.appendChild(div);
  chatBox.scrollTop = chatBox.scrollHeight;
}


function speak(text) {
  if (!('speechSynthesis' in window)) return;

  speechSynthesis.cancel();

  const utterance = new SpeechSynthesisUtterance(text);


  utterance.lang = "en-US";
  utterance.rate = 0.9;
  utterance.pitch = 1;
  utterance.volume = 1;

  const voices = speechSynthesis.getVoices();

  const englishVoice = voices.find(v =>
    v.lang.startsWith("en")
  );

  if (englishVoice) {
    utterance.voice = englishVoice;
  }

  speechSynthesis.speak(utterance);
}

async function sendMessage() {
  const message = userInput.value.trim();
  if (!message) return;

  addMessage(message, "user");
  userInput.value = "";

  //robot.style.animation = 'breathing 0.8s infinite';

  try {
    const res = await fetch('https://nava-ai-assistant.onrender.com/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message })
    });

    const data = await res.json();
    const botReply = data.response || "Sorry, samajh nahi aaya.";

    addMessage(botReply, "bot");
    speak(botReply);

  } catch (e) {
    addMessage("Backend connect nahi ho raha!", "bot");
  }

  //setTimeout(() => {
  //  //robot.style.animation = 'breathing 4s infinite ease-in-out';
  //}, 2500);
}

function startVoiceInput() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    alert("Voice input ke liye Chrome use karo.");
    return;
  }

  const recognition = new SpeechRecognition();
  recognition.lang = 'hi-IN';
  recognition.onresult = (event) => {
    userInput.value = event.results[0][0].transcript;
    sendMessage();
  };
  recognition.start();
}

function clearChat() {
  chatBox.innerHTML = '';
}

userInput.addEventListener("keypress", (e) => {
  if (e.key === "Enter") sendMessage();
});