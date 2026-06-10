# Flow — Recall refinement turn (U.3)

Second and later turns within a conversation. The user types a
clarification like "no, the one from last week" and we want to reuse
the candidate set from the first turn instead of re-retrieving from
scratch — that's the "no fresh retrieval" branch.

```mermaid
sequenceDiagram
    autonumber
    participant U as User<br/>(Remember tab)
    participant Ext as recall.js
    participant API as FastAPI<br/>POST /recall
    participant Rec as Recaller
    participant Conv as ConversationStore<br/>(TTL 30 min)
    participant Son as Sonnet 4.5

    Note over U,Ext: First turn already happened —<br/>conversation_id is held in popup memory

    U->>Ext: types refinement<br/>("no, last week")
    Ext->>API: POST /recall<br/>{query, conversation_id: "abc…"}
    API->>Rec: recall(query, conv_id)

    Rec->>Conv: get(conv_id)
    Conv-->>Rec: prior {query, candidates,<br/>chosen_ids, turn_count}

    alt Conv hit + recent
        Note over Rec: "no fresh retrieval" branch.<br/>Reuse the same candidate list,<br/>just ask Sonnet to re-rank<br/>with the refinement applied.
        Rec->>Son: refine prompt<br/>(prior candidates +<br/>prior query + new query)
        Son-->>Rec: tighter ranked list
    else Conv miss or expired
        Note over Rec: Fall back to first-turn flow:<br/>full RetrievalService + rerank.<br/>Treat as new conversation.
        Rec->>Rec: full retrieve + rerank<br/>(see flow-recall.md)
    end

    Rec->>Conv: update {query, chosen_ids,<br/>turn_count++}
    Rec-->>API: RecallResponse<br/>(same conv_id)
    API-->>Ext: 200 JSON
    Ext->>U: re-renders cards<br/>(narrowed list)
```

## Why "no fresh retrieval" is safer than re-running everything

- The candidate set was already curated by RRF + Sonnet on turn 1.
  Re-retrieving with a refinement like "last week" would skew the
  ranker toward temporal recency at the cost of topic match.
- Cheaper: skips Chroma + FTS5 calls entirely on refinement turns.
- Predictable: the user expects the list to narrow, not change topic.

## Conversation lifetime

- In-process dict, TTL 30 min, no DB persistence.
- Popup close → conversation_id forgotten on the client side; the
  backend entry expires on TTL.
- "Start over" button on the popup nukes `conversationId` and treats
  the next query as turn 1.
