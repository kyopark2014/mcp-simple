# Knowledge Base with S3 Vector

여기에서는 AWS의 완전관리형 RAG 서비스인 Knowledge Base를 생성하는 방법에 대해 설명합니다.

1. 문서를 저장하기 위해 Amazon S3를 생성합니다. [Amazon S3 Console](https://us-west-2.console.aws.amazon.com/s3/home?region=us-west-2#)에 접속해서, [Create bucket]을 선택한 후에 아래와 같이 [Bucket name]을 입력하고 나머지 설정은 그대로 유지한 상태에서 [Create]를 선택합니다. 이때 bucket의 이름은 반드시 unique한 이름이어야 합니다.

<img width="480" height="91" alt="image" src="https://github.com/user-attachments/assets/84b45e0d-21fd-4174-8de2-e809812bf939" />

2. [Knolwedge Base Console](https://us-west-2.console.aws.amazon.com/bedrock/home?region=us-west-2#/knowledge-bases)에 접속하여 [Create]를 선택합니다. 이때, 아래와 같이 [Knowledbe Base with vector store]를 선택합니다.

<img width="615" height="205" alt="image" src="https://github.com/user-attachments/assets/756ef7c4-720a-4817-8864-d4ce31413a3e" />

3. 아래와 같이 이름을 입력합니다. 이후 하단으로 이동하여 [Next]를 선택합니다. 이때 [IAM permissions]은 기본값인 "Create and use a new service role"이 선택되고, [Choose data source type]은 "Amazon S3"가 선택됩니다.

<img width="599" height="130" alt="image" src="https://github.com/user-attachments/assets/8b83272b-d709-4dbf-a77e-586c6e3a0cfb" />

4. [Configure data source]의 [S3 URI]에서 아래와 같이 생성한 Amazon S3를 선택합니다. 이후 나머지 설정은 유지한 상태에서 하단의 [Next]를 선택합니다. 

<img width="329" height="82" alt="image" src="https://github.com/user-attachments/assets/aa0ebb10-3f2c-4001-818f-e68b5b25424c" />

5. 아래와 같이 Embedding model로 "Titan Text Embedding V2"를 선택하고 [Apply]를 선택합니다. 

<img width="804" height="284" alt="image" src="https://github.com/user-attachments/assets/b47f3e60-5c29-4ebd-a6f1-bacc678fccbb" />

6. [Vector store type]로 아래와 같이 "Amazon S3 Vectors"를 선택하고, 하단의 [Next]를 선택한 다음에 [Create Knowledge Base]를 선택하여 생성합니다.

<img width="618" height="103" alt="image" src="https://github.com/user-attachments/assets/3d9aeac2-9e5b-4e62-8116-e6353945b6c2" />

7. 생성이 되면 아래와 같이 생성한 [Knowledge Base overview]에서 Knowledge Base ID를 확인할 수 있습니다.

<img width="166" height="55" alt="image" src="https://github.com/user-attachments/assets/46483548-db1f-45fb-bbdf-f2bac97dcc95" />
