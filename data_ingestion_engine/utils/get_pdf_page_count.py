# Description: Get the number of pages in a PDF file with enhanced error handling.
def get_pdf_page_count(pdf_path):
    """
    Get the number of pages in a PDF file with enhanced error handling.

    Args:
        pdf_path (str): Path to the PDF file

    Returns:
        int: Number of pages in the PDF, or -1 if an error occurred
    """
    print(f"Attempting to count pages in PDF: {pdf_path}")

    # Method 1: Using PyPDF2
    try:
        from PyPDF2 import PdfReader
        with open(pdf_path, 'rb') as file:
            try:
                # Use strict=False for more lenient parsing
                reader = PdfReader(file, strict=False)
                count = len(reader.pages)
                print(f"PyPDF2 counted {count} pages")
                return count
            except Exception as e:
                print(f"PyPDF2 failed: {e}")
    except ImportError:
        print("PyPDF2 not available")

    # Method 2: Using pypdf
    try:
        from pypdf import PdfReader
        with open(pdf_path, 'rb') as file:
            try:
                reader = PdfReader(file)
                count = len(reader.pages)
                print(f"pypdf counted {count} pages")
                return count
            except Exception as e:
                print(f"pypdf failed: {e}")
    except ImportError:
        print("pypdf not available")

    # Method 3: Using pikepdf
    try:
        import pikepdf
        try:
            with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
                count = len(pdf.pages)
                print(f"pikepdf counted {count} pages")
                return count
        except Exception as e:
            print(f"pikepdf failed: {e}")
    except ImportError:
        print("pikepdf not available")

    # Method 4: Using pdfplumber
    try:
        import pdfplumber
        try:
            with pdfplumber.open(pdf_path) as pdf:
                count = len(pdf.pages)
                print(f"pdfplumber counted {count} pages")
                return count
        except Exception as e:
            print(f"pdfplumber failed: {e}")
    except ImportError:
        print("pdfplumber not available")

    # Method 5: Using pdfrw
    try:
        from pdfrw import PdfReader
        try:
            reader = PdfReader(pdf_path)
            count = len(reader.pages)
            print(f"pdfrw counted {count} pages")
            return count
        except Exception as e:
            print(f"pdfrw failed: {e}")
    except ImportError:
        print("pdfrw not available")

    # If we get here, all methods failed
    print("All PDF reading methods failed. The file may be corrupted or not a valid PDF.")

    # Last resort: Try to read binary and count '%Page' markers
    try:
        with open(pdf_path, 'rb') as file:
            content = file.read()
            # Look for page markers in binary content
            page_markers = content.count(b'/Page')
            if page_markers > 0:
                print(f"Counted {page_markers} page markers in binary content")
                return page_markers
    except Exception as e:
        print(f"Binary reading failed: {e}")

    return -1
