"""
Collect Hugging Face PTMs in the database.
"""

from utils.HFConfig import HuggingFaceConfig
from utils.DBConfig import DatabaseConfig
from utils.DBSchema import DBTableNames as DBT, DBFieldNames as DBF
from huggingface_hub import HfApi

def insert_model_into_db(model):
    model_id = model._id
    full_name = model.id
    name = full_name.split("/")[-1]  # Extract the model name from the full name
    author = model.author
    sha = model.sha
    downloads = model.downloads
    likes = model.likes
    created_at = model.created_at
    last_modified_date = model.last_modified
    private = model.private
    gated = model.gated
    pipeline_tag = model.pipeline_tag
    library_name = model.library_name

    data = {
        DBF.Models.MODEL_ID: model_id,
        DBF.Models.FULL_NAME: model.full_name,
        DBF.Models.NAME: name,
        DBF.Models.AUTHOR: author,
        DBF.Models.SHA: sha,
        DBF.Models.DOWNLOADS: downloads,
        DBF.Models.LIKES: likes,
        DBF.Models.CREATED_AT: created_at,
        DBF.Models.LAST_MODIFIED_DATE: last_modified_date,
        DBF.Models.PRIVATE: private,
        DBF.Models.GATED: gated,
        DBF.Models.PIPELINE_TAG: pipeline_tag,
        DBF.Models.LIBRARY_NAME: library_name,
    }
    print(f"Inserting model {name} into the database...")
    db_config.insert_to_db(DBT.MODELS.value, data)



if __name__ == "__main__":
    db_config  = DatabaseConfig()
    connection, cursor = db_config.create_db_connection()
    
    hf_config = HuggingFaceConfig()
    hf_config.init_huggingface_access()

    hf_api = HfApi(token="{}".format(hf_config.token))
    print("Fetching models from Hugging Face...")
    models = hf_api.list_models(sort="downloads", full=True, direction=-1)

    for model in models:
        insert_model_into_db(model)

    print("Done collecting models from Hugging Face.")


