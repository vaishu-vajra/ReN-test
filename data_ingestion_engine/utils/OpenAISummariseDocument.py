import os
import time
from openai import OpenAI
from dotenv import load_dotenv
from data_ingestion_engine.utils.getInstruction import get_instruction
from data_ingestion_engine.utils.store_data import patch_report

load_dotenv()


def stream_response(token: str, instruction: str, secondary_prompt: str, file_path: str, company_id: str, report_id: str, fallback_threshold: int = 200):
    OPENAI_API_KEY_2 = os.getenv('OPENAI_API_KEY')
    client = OpenAI(api_key=OPENAI_API_KEY_2)
    prompt = get_instruction(instruction)

    # Create assistant and vector store as before.
    assistant = client.beta.assistants.create(
        name=f"DEI PDF Processor - {company_id}",
        instructions=prompt,
        model="gpt-4o-mini",
        tools=[{"type": "file_search"}],
    )
    vector_store = client.beta.vector_stores.create(
        name=f"DEI PDF Processor - {company_id}"
    )

    with open(file_path, "rb") as f:
        file_streams = [f]
        file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
            vector_store_id=vector_store.id, files=file_streams
        )
    print("Vector store upload status:", file_batch.status)
    print("File counts:", file_batch.file_counts)

    retries = 10
    delay = 5  # seconds
    for _ in range(retries):
        if file_batch.file_counts.completed == len(file_streams):
            break
        print("Waiting for file indexing to complete...")
        time.sleep(delay)

    assistant = client.beta.assistants.update(
        assistant_id=assistant.id,
        tool_resources={"file_search": {
            "vector_store_ids": [vector_store.id]}},
    )
    message_file = client.files.create(
        file=open(file_path, "rb"),
        purpose="assistants",
    )
    if instruction == "summary":
        thread = client.beta.threads.create(
            messages=[
                {
                    "role": "user",
                    "content": "You are a document summarizer. Provide a detailed Markdown summary of the document.",
                    "attachments": [{"file_id": message_file.id, "tools": [{"type": "file_search"}]}],
                }
            ]
        )
    else:
        thread = client.beta.threads.create(
            messages=[
                {
                    "role": "user",
                    "content": "You are a data extractor. Provide data points from the document.",
                    "attachments": [{"file_id": message_file.id, "tools": [{"type": "file_search"}]}],
                }
            ]
        )

    def run_stream():
        stream = client.beta.threads.runs.create(
            thread_id=thread.id, assistant_id=assistant.id, stream=True
        )
        aggregated = ""
        for event in stream:
            if hasattr(event, "data") and event.data:
                if hasattr(event.data, "delta") and event.data.delta:
                    if hasattr(event.data.delta, "content") and event.data.delta.content:
                        for block in event.data.delta.content:
                            if hasattr(block, "text") and block.text:
                                text_value = getattr(block.text, "value", "")
                                if text_value:
                                    print("Content:", text_value)
                                    aggregated += text_value
                                    yield text_value
            else:
                yield str(event)
        return aggregated

    # First pass to aggregate summary
    aggregated_response = ""
    output_chunks = []
    for chunk in run_stream():
        output_chunks.append(chunk)
        aggregated_response += chunk

    # Check if the aggregated response is too short, then use fallback prompt.
    if len(aggregated_response.strip()) < fallback_threshold:
        print("Aggregated summary/data points too short, invoking fallback prompt...")
        fallback_prompt = secondary_prompt
        client.beta.threads.messages.create(
            thread_id=thread.id,
            role="user",
            content=fallback_prompt,
        )
        aggregated_response = ""
        output_chunks = []
        for chunk in run_stream():
            output_chunks.append(chunk)
            aggregated_response += chunk
        if len(aggregated_response.strip()) < fallback_threshold:
            raise Exception(
                "Fallback extraction failed: insufficient content.")

    # Store the summary in the database using patch_report_summary.
    if patch_report(report_id, aggregated_response, instruction, token):
        print("Summary/Data points stored successfully.")
    else:
        print("Failed to store summary/data points in the database.")

    # Clean up the local file.
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception as cleanup_error:
        print(f"Error deleting file {file_path}: {cleanup_error}")

    # Finally, yield the summary chunks.
    for chunk in output_chunks:
        yield chunk
