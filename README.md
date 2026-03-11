```mermaid
graph TD
    A[Webhook Tally/Form] --> B(generar_payload: Analizar Votos)
    
    subgraph Logica_Voto [1. Criterios de Calificación]
        B --> C{¿Gatekeepers S1, S2, S7?}
        C -- "Algún 🔴 o Vacío" --> KO_Veto[Veredicto: 🔴 KO / VETO]
        C -- "Todos 🟢" --> D{¿Compensadores S3-S6?}
        
        D -- "Menos de 2 🟢" --> KO_Comp[Veredicto: 🔴 KO]
        D -- "2 o más 🟢" --> OK_Vote[Veredicto: ✅ OK]
    end

    subgraph Identificacion [2. Match en Attio]
        OK_Vote & KO_Veto & KO_Comp --> E[Buscar Compañía por Dominio]
        E --> F[Buscar Deal Asociado]
        F --> G[Obtener Entry y Tier Actual]
    end

    subgraph Funnel [3. Decisión de Funnel]
        G --> H{Cálculo de Status}
        
        %% Lógica Tier 1
        H -->|Tier 1| T1_Eval{Votos T1}
        T1_Eval -->|2 OK| J[Status: First Interaction]
        T1_Eval -->|2 KO| K[Status: Killed]
        T1_Eval -->|1 OK + 1 KO| L[Escalar: Cambiar a Tier 2]
        
        %% Lógica Tier 2
        H -->|Tier 2| T2_Eval{Votos T2}
        T2_Eval -->|2 OK| J
        T2_Eval -->|2 KO| K
    end

    subgraph Persistencia [4. Update Attio]
        J & K & L --> M[Acumular Payloads y Flags]
        M --> N[PATCH Entry: Status + Qualified Signals]
    end

    %% Estilos de Veredicto
    style OK_Vote fill:#d4edda,stroke:#155724
    style KO_Veto fill:#f8d7da,stroke:#721c24
    style KO_Comp fill:#f8d7da,stroke:#721c24
    style L fill:#fff3cd,stroke:#856404
