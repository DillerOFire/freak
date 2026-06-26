import asyncio
import os
import sys
import logging
from unittest.mock import AsyncMock, patch

# Ensure the root folder is in the python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set up basic logging to see LLM output details
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# Import functions after path is set
from bot.llm import generate_response, get_system_prompt, generate_reaction

async def main():
    print("=== Bot Prompt Tester ===")
    
    # Mock data representing a typical Telegram conversation
    messages_context = [
        {"message_id": 100, "sender": "Vasya", "user_id": 111, "text": "Hey everyone! What are you up to?"},
        {"message_id": 101, "sender": "Petya", "user_id": 222, "text": "Reading a book on local history."},
        {"message_id": 102, "sender": "Vasya", "user_id": 111, "text": "Nice. Do you think the official account is accurate?"},
    ]
    
    user_thoughts = {
        "Vasya": "Casual chatter.",
        "Petya": "Often shares book recommendations."
    }
    
    general_memories = [
        "Topic: Local history, Summary: The group discussed history books and source reliability."
    ]
    
    # Patch database calls to avoid hitting the real database config tables
    with patch("bot.llm.get_config", AsyncMock(return_value=None)), \
         patch("bot.llm.set_config", AsyncMock()), \
         patch("bot.llm.update_user_thought", AsyncMock()) as mock_update_thought, \
         patch("bot.llm.add_general_memory", AsyncMock()) as mock_add_memory:
        
        # Test 1: Get the system prompt
        sys_prompt = await get_system_prompt()
        print("\n--- Current System Prompt ---")
        print(sys_prompt)
        print("-----------------------------\n")
        
        # Test 2: Generate response with focus on the last message
        print("Sending request to LLM...")
        result = await generate_response(
            messages_context=messages_context,
            user_thoughts=user_thoughts,
            general_memories=general_memories,
            chat_id=9999,
            focus_message_id=102
        )
        
        print("\n--- LLM Output Result ---")
        if result:
            print(f"Reply to Message ID: {result.get('reply_to_message_id')}")
            print(f"Reply Content: {result.get('content')}")
            
            tool_calls = result.get("tool_calls", [])
            print(f"Tool Calls ({len(tool_calls)}):")
            for tc in tool_calls:
                print(f"  - {tc.get('name')}: {tc.get('arguments')}")
        else:
            print("No response or error occurred.")
        print("-------------------------\n")
        
        # Test 3: Generate reaction
        print("Generating reaction to Vasya's message...")
        reaction = await generate_reaction("Do you think the official account is accurate?")
        print(f"Generated reaction: {reaction}")
        print("-------------------------\n")

if __name__ == "__main__":
    asyncio.run(main())
