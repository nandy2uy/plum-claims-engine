from src.models.medical import ExtractedPrescription, ExtractedBill

# This is a placeholder for the live OpenAI integration.
# When you're ready to spend actual API credits, you drop the real client here.

async def extract_prescription_via_llm(image_url: str) -> ExtractedPrescription:
    raise NotImplementedError("Live LLM API not configured yet. Use mock data.")

async def extract_bill_via_llm(image_url: str) -> ExtractedBill:
    raise NotImplementedError("Live LLM API not configured yet. Use mock data.")