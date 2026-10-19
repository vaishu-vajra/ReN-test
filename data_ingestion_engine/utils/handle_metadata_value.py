import math
import re
import unidecode

def handle_metadata_value(value):
    """Convert float or nan to string, handle missing values, and remove non-ASCII characters."""
    if isinstance(value, float):
        if math.isnan(value):
            return ""  # Replace NaN with empty string
        else:
            return str(int(value))  # Convert float to string (without decimal point if it's a whole number)
    
    # Convert other types to string
    value = str(value)  

    # Remove non-ASCII characters using unidecode
    value = unidecode.unidecode(value)

    # Remove any unwanted characters (newlines, tabs, etc.)
    value = re.sub(r'[\n\t\r]', ' ', value)  # Replace newlines and tabs with a space
    value = value.strip()  # Remove leading/trailing spaces

    return value