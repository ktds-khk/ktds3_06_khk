import os
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    SearchIndex,
    SearchField,
    SimpleField,
    SearchableField,
    SearchFieldDataType,
    VectorSearch,
    HnswAlgorithmConfiguration,
    VectorSearchProfile,
    SemanticConfiguration,
    SemanticPrioritizedFields,
    SemanticField,
    SemanticSearch
)
from azure.core.credentials import AzureKeyCredential
from dotenv import load_dotenv

# 환경 변수 로드
load_dotenv()

# Azure 설정
SEARCH_ENDPOINT = os.getenv("SEARCH_ENDPOINT")
SEARCH_KEY = os.getenv("SEARCH_KEY")
INDEX_NAME = "ito-events-index"

def create_search_index():
    """AI Search 인덱스 생성"""
    
    # 인덱스 클라이언트 생성
    index_client = SearchIndexClient(
        endpoint=SEARCH_ENDPOINT,
        credential=AzureKeyCredential(SEARCH_KEY)
    )
    
    # 필드 정의
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True),
        SearchableField(name="event_id", type=SearchFieldDataType.String, filterable=True),
        SearchableField(name="timestamp", type=SearchFieldDataType.DateTimeOffset, filterable=True, sortable=True),
        SearchableField(name="event_type", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchableField(name="severity", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchableField(name="source", type=SearchFieldDataType.String, filterable=True),
        SearchableField(name="message", type=SearchFieldDataType.String),
        SearchableField(name="description", type=SearchFieldDataType.String),
        SimpleField(name="host", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="service", type=SearchFieldDataType.String, filterable=True),
        SearchableField(name="tags", type=SearchFieldDataType.Collection(SearchFieldDataType.String), filterable=True),
        
        # 분석 결과 필드
        SearchableField(name="analyzed_category", type=SearchFieldDataType.String, filterable=True),
        SearchableField(name="root_cause", type=SearchFieldDataType.String),
        SearchableField(name="correlation_id", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="anomaly_score", type=SearchFieldDataType.Double, filterable=True, sortable=True),
        
        # 벡터 검색을 위한 필드
        SearchField(
            name="content_vector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=1536,
            vector_search_profile_name="myHnswProfile"
        ),
    ]
    
    # 벡터 검색 구성
    vector_search = VectorSearch(
        algorithms=[
            HnswAlgorithmConfiguration(
                name="myHnsw",
                parameters={
                    "m": 4,
                    "efConstruction": 400,
                    "efSearch": 500,
                    "metric": "cosine"
                }
            )
        ],
        profiles=[
            VectorSearchProfile(
                name="myHnswProfile",
                algorithm_configuration_name="myHnsw"
            )
        ]
    )
    
    # 시맨틱 검색 구성
    semantic_config = SemanticConfiguration(
        name="default",
        prioritized_fields=SemanticPrioritizedFields(
            title_field=SemanticField(field_name="event_type"),
            content_fields=[
                SemanticField(field_name="message"),
                SemanticField(field_name="description")
            ],
            keywords_fields=[
                SemanticField(field_name="tags"),
                SemanticField(field_name="severity")
            ]
        )
    )
    
    semantic_search = SemanticSearch(configurations=[semantic_config])
    
    # 인덱스 생성
    index = SearchIndex(
        name=INDEX_NAME,
        fields=fields,
        vector_search=vector_search,
        semantic_search=semantic_search
    )
    
    # 기존 인덱스가 있으면 삭제
    try:
        index_client.delete_index(INDEX_NAME)
        print(f"기존 인덱스 '{INDEX_NAME}' 삭제됨")
    except:
        pass
    
    # 새 인덱스 생성
    result = index_client.create_index(index)
    print(f"인덱스 '{result.name}' 생성 완료!")
    
    # 인덱스 정보 출력
    print("\n인덱스 필드:")
    for field in result.fields:
        print(f"  - {field.name}: {field.type}")

if __name__ == "__main__":
    create_search_index()