import warnings
import os

# Suppress python-dotenv parsing warnings before importing anything
warnings.filterwarnings("ignore", message=".*python-dotenv.*", category=UserWarning)
warnings.filterwarnings("ignore", message=".*dotenv.*", category=UserWarning)

# Suppress Pydantic V2 config warnings
warnings.filterwarnings("ignore", message=".*Valid config keys have changed in V2.*", category=UserWarning)
warnings.filterwarnings("ignore", message=".*underscore_attrs_are_private.*", category=UserWarning)

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8001,
        reload=True,
    )
