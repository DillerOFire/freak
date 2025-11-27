import logging
from openai import AsyncOpenAI
import json
from config import OPENROUTER_API_KEY, OPENROUTER_MODEL
from bot.memory import update_user_thought, add_general_memory

client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

SYSTEM_PROMPT = """
You are a participant in a Telegram group chat. 
Your goal is to be a natural, engaging member of the group.
You have access to memories about users and general topics.
You can update these memories using the provided tools.

When you receive a list of recent messages:
1. Analyze the conversation.
2. Update your thoughts about users if you learn something new or your opinion changes. **IMPORTANT: If a user reveals a role (e.g., Admin) or a key fact, you MUST update your thought about them immediately.**
3. Add to general memory if a new important topic is discussed.
4. Decide if you should reply to any of the messages. 
   - You don't always have to reply.
   - If you reply, specify which message ID you are replying to (or None for a general message).
   - Your reply should be casual, relevant, and fit the group vibe.

Output your response as a JSON object with the following structure:
{
  "reply_to_message_id": <message_id or null>,
  "content": "<your reply text>"
}
If you choose NOT to reply, return null for content.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "update_user_thought",
            "description": "Update your internal thoughts/opinion about a user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "The user's ID"},
                    "username": {"type": "string", "description": "The user's username"},
                    "thought": {"type": "string", "description": "The new thought/summary about the user"},
                },
                "required": ["user_id", "username", "thought"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_general_memory",
            "description": "Add a new general memory about a topic.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "The topic of the memory"},
                    "summary": {"type": "string", "description": "The summary of the memory"},
                },
                "required": ["topic", "summary"],
            },
        },
    },
]

async def generate_response(messages_context: list[dict], user_thoughts: dict, general_memories: list[str]) -> dict | None:
    # Construct the full context
    context_str = "Recent Messages:\n"
    for msg in messages_context:
        context_str += f"[{msg['message_id']}] {msg['sender']}: {msg['text']}\n"
    
    context_str += "\nThoughts about Users:\n"
    for username, thought in user_thoughts.items():
        context_str += f"{username} - {thought}\n"
        
    context_str += "\nGeneral Memories:\n"
    for mem in general_memories:
        context_str += f"- {mem}\n"

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": context_str}
    ]

    try:
        logging.info("Sending prompt to LLM:")
        for msg in messages:
            logging.info(f"Role: {msg['role']}")
            logging.info(f"Content:\n{msg['content']}")
            logging.info("-" * 20)
        response = await client.chat.completions.create(
            model=OPENROUTER_MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            response_format={"type": "json_object"} 
        )

        message = response.choices[0].message
        
        # Log the raw response content
        if message.content:
            logging.info(f"LLM Response Content: {message.content}")

        # Handle tool calls
        if message.tool_calls:
            for tool_call in message.tool_calls:
                if tool_call.function.name == "update_user_thought":
                    args = json.loads(tool_call.function.arguments)
                    logging.info(f"Memorizing (User Thought): {json.dumps(args, ensure_ascii=False)}")
                    await update_user_thought(args["user_id"], args["username"], args["thought"])
                elif tool_call.function.name == "add_general_memory":
                    args = json.loads(tool_call.function.arguments)
                    logging.info(f"Memorizing (General): {json.dumps(args, ensure_ascii=False)}")
                    await add_general_memory(args["topic"], args["summary"])

        # Parse the JSON content for the reply
        if message.content:
            try:
                content_json = json.loads(message.content)
                if content_json.get("content"):
                    return content_json
            except json.JSONDecodeError:
                logging.error(f"Failed to parse JSON response: {message.content}")
                return None
                
        return None

    except Exception as e:
        logging.error(f"Error in generate_response: {e}")
        return None
