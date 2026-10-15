#!/usr/bin/env python
from pydantic import BaseModel, Field

from crewai.flow import Flow, listen, start
from crewai import Agent, LLM

from medisense_flow.tools.custom_tool import MedicalRAGTool
from datetime import datetime

# ---- Local LLM (Ollama) ----
# Make sure Ollama is running and the model is pulled:  ollama pull llama3.1
# The model name must match `ollama list`, prefixed with "ollama/".
LOCAL_MODEL = "ollama/llama3.1"
OLLAMA_URL = "http://localhost:11434"

local_llm = LLM(
    model=LOCAL_MODEL,
    base_url=OLLAMA_URL,
    temperature=0.2,  # low temperature = more consistent medical output
)

class ContentState(BaseModel):
    diagnostics_init: str = ""
    rediagnosis: str = ""
    query: str = ""
    q1: str = ""
    q2: str = ""
    q3: str = ""
    score: int = 0

class DiagnosisScore(BaseModel):
    score: int = Field(
        description=(
            "The overall quality score of the re-diagnosis, as a whole number "
            "from 1 (poor) to 10 (excellent), based on clarity, completeness, "
            "reasoning quality, safety, and appropriate caution."
        ),
        ge=1,
        le=10,
    )

class FollowUpQuestions(BaseModel):
    question_1: str = Field(
        description=(
            "The most important follow-up question, aimed at clarifying the "
            "main symptom or the biggest uncertainty in the initial diagnosis. "
            "Must be clear, concise, one idea only, and in plain language."
        )
    )
    question_2: str = Field(
        description=(
            "A follow-up question about associated symptoms, duration, severity, "
            "or triggers that helps differentiate between the suggested conditions. "
            "Must be clear, concise, one idea only, and in plain language."
        )
    )
    question_3: str = Field(
        description=(
            "A follow-up question about medical history, medications, lifestyle, "
            "or red-flag symptoms that could indicate the need for urgent care. "
            "Must be clear, concise, one idea only, and in plain language."
        )
    )

class ContentFlow(Flow[ContentState]):

    @start()
    def reportinput(self):
        diagnostic_input = input("Please enter your medical query: ")

        self.state.query = diagnostic_input

        return self.state.query

    @listen(reportinput)
    def rag_task(self, query):
        researcher = Agent(
            role="Medical Diagnostic Researcher",
            llm=local_llm,
            goal=(
                "Analyze the user's query, retrieve relevant medical information "
                "from the vector database using the RAG tool, and provide "
                "evidence-based diagnostic insights based on the retrieved information."
            ),
            backstory=(
                "You are an AI-powered medical research assistant specializing in "
                "analyzing user-reported symptoms and retrieving relevant medical "
                "knowledge from a vector database. You use the provided RAG tool "
                "to find information related to symptoms, possible conditions, "
                "risk factors, and recommended next steps. You base your responses "
                "on the retrieved information, clearly communicate uncertainty, "
                "and avoid presenting potential conditions as confirmed diagnoses. "
                "You encourage users to consult qualified healthcare professionals "
                "for medical evaluation."
            ),
            tools=[MedicalRAGTool()],
            verbose=True,
        )
        prompt = f"""
    Analyze the following user query: {self.state.query}

    Use the RAG tool to retrieve relevant medical information from the vector
    database. Based on the retrieved information, provide:

    1. Relevant symptoms and their possible medical significance.
    2. Potential conditions associated with the reported symptoms.
    3. Relevant risk factors or supporting information found in the retrieved data.
    4. Recommended next steps and whether professional medical evaluation is advisable.

    Base your response on the retrieved information. Do not invent medical facts
    or present potential conditions as confirmed diagnoses. If the retrieved
    information is insufficient, clearly state the limitations.

    Format your response in a clear, structured, and user-friendly manner.
    """
        result = researcher.kickoff(prompt)
        self.state.diagnostics_init = result.raw
        return result.raw


    @listen(rag_task)
    def followback_questions(self, diagnostics_init_opt):
        followup_agent = Agent(
            role="Medical Diagnostic Follow-up Specialist",
            llm=local_llm,
            goal=(
                "Analyze the initial diagnostic insights and generate follow-up "
                "questions to gather additional information from the user."
            ),
            backstory=(
                "You are an AI-powered medical diagnostic assistant specializing in "
                "analyzing initial diagnostic insights and generating follow-up "
                "questions to gather more information from users. You aim to clarify "
                "uncertainties, explore potential conditions, and ensure a comprehensive "
                "understanding of the user's symptoms. Your questions should be clear, "
                "concise, and relevant to the user's reported symptoms and the initial "
                "diagnostic insights."
            ),
            verbose=True,
        )
        prompt = f"""You are given the initial diagnostic insights produced by another agent:

--- INITIAL DIAGNOSTIC INSIGHTS ---
{diagnostics_init_opt}
--- END OF INSIGHTS ---

Your task is to generate follow-up questions that will help narrow down and
validate (or rule out) the possible conditions listed above.

Step 1 - Analyze:
- Identify the symptoms the user has already reported.
- Identify the conditions or possibilities suggested in the insights.
- Note any missing, vague, or uncertain information (e.g., onset, duration,
  severity, triggers, associated symptoms, medical history, medications,
  allergies, lifestyle factors, recent travel or exposures).

Step 2 - Generate questions:
- Write exactly 3 follow-up questions, ordered from most to least important for
  differentiating between the suggested conditions.
- Cover the gaps from Step 1. Where relevant, include questions about red-flag
  symptoms that would indicate urgent care is needed.
- Each question must be clear, concise, one idea at a time, and written in plain,
  non-technical language that a layperson can answer.
- Do NOT repeat information the user has already provided.
- Do NOT ask for personal identifying information (name, address, ID numbers).
- Prefer questions that can be answered with short answers (yes/no, a number,
  a choice, or a brief description)."""
        result = followup_agent.kickoff(prompt, response_format=FollowUpQuestions)
        if result.pydantic:
            questions = result.pydantic  # FollowUpQuestions instance
            self.state.q1 = questions.question_1
            self.state.q2 = questions.question_2
            self.state.q3 = questions.question_3
        return result.raw

    @listen(followback_questions)
    def human_feedback(self,):
        print("\n--- Follow-up Questions ---")
        print(f"1. {self.state.q1}")
        ans1 = input("Please answer question 1: ")
        print(f"2. {self.state.q2}")
        ans2 = input("Please answer question 2: ")
        print(f"3. {self.state.q3}")
        ans3 = input("Please answer question 3: ")

        return ans1, ans2, ans3

    @listen(human_feedback)
    def rediagnosis(self, answer):
        ans1, ans2, ans3 = answer
        formatted_answers = (
            f"Answer 1: {ans1}\n"
            f"Answer 2: {ans2}\n"
            f"Answer 3: {ans3}"
        )
        rediagnosis_agent = Agent(
            role="Medical Re-Diagnosis Specialist",
            llm=local_llm,
            goal=(
                "Re-evaluate the initial diagnosis using the user's answers to the "
                "follow-up questions, and provide an updated, more accurate diagnostic "
                "assessment."
            ),
            backstory=(
                "You are an AI-powered medical diagnostic assistant specializing in "
                "refining preliminary diagnoses. You carefully compare the initial "
                "diagnostic insights with new information gathered from follow-up "
                "questions, identify which conditions become more or less likely, and "
                "produce a clear, evidence-based updated assessment. You are cautious, "
                "transparent about uncertainty, and always prioritize patient safety "
                "by flagging symptoms that need urgent medical attention."
            ),
        )
        prompt = f"""
You are given the initial diagnosis, the follow-up questions that were asked,
and the user's answers.

--- INITIAL DIAGNOSIS ---
{self.state.diagnostics_init}

--- FOLLOW-UP QUESTIONS ---
Question 1: {self.state.q1}
Question 2: {self.state.q2}
Question 3: {self.state.q3}

--- USER'S ANSWERS ---
{formatted_answers}

Your task is to provide a re-diagnosis.

Step 1 - Review:
- Match each answer to its question.
- Identify which new facts support, weaken, or contradict each condition in
  the initial diagnosis.

Step 2 - Re-evaluate:
- Update the likelihood of each condition (more likely, less likely, or
  unchanged) and briefly explain why, citing the user's answers.
- If the answers point to a condition not in the initial diagnosis, include it
  and explain why.
- Identify any remaining uncertainty or missing information.

Step 3 - Safety:
- If any answer indicates red-flag symptoms, clearly state that the user
  should seek urgent or emergency medical care.
- Recommend consulting a qualified healthcare professional for confirmation.

Constraints:
- Do NOT invent symptoms or details that are not in the inputs.
- Do NOT state absolute certainty; use cautious language such as "most likely"
  or "possible".
- Do NOT prescribe specific medications or dosages.
- If an answer is vague, missing, or irrelevant, say so and treat it as
  unknown rather than guessing.

Output format - respond with:
1. Most likely condition(s), ranked by likelihood
2. Reasoning for each, based on the user's answers
3. What changed compared to the initial diagnosis
4. Remaining uncertainties
5. Recommended next steps, including when to seek urgent care
"""
        result = rediagnosis_agent.kickoff(prompt)
        self.state.rediagnosis = result.raw
        return result.raw

    @listen(rediagnosis)
    def diagnosis_scoring(self, result):
        scoring_agent = Agent(
            role="Medical Diagnosis Scoring Specialist",
            llm=local_llm,
            goal=(
                "Evaluate the quality, clarity, and usefulness of the re-diagnosis "
                "provided by the previous agent, and provide a score and feedback."
            ),
            backstory=(
                "You are an AI-powered medical diagnostic evaluation assistant. "
                "You assess the accuracy, clarity, and completeness of diagnostic "
                "assessments produced by other agents. You provide constructive "
                "feedback and a score to help improve future diagnostic outputs."
            ),
            verbose=True,
        )
        prompt = f"""You are given a re-diagnosis produced by another agent:

--- RE-DIAGNOSIS ---
{result}
--- END OF RE-DIAGNOSIS ---

Your task is to evaluate the quality of this re-diagnosis and provide a score
with feedback.

Step 1 - Review:
- Read the re-diagnosis above carefully.
- Identify the conditions it lists, the reasoning given, the changes it notes
  compared to the initial diagnosis, and the next steps it recommends.

Step 2 - Score each criterion from 1 (poor) to 10 (excellent):
- Clarity: Is the re-diagnosis easy to read and understand for a layperson?
- Completeness: Does it cover likely conditions, reasoning, what changed,
  remaining uncertainties, and next steps?
- Reasoning quality: Is each conclusion clearly supported by the information
  provided, without invented symptoms or details?
- Safety: Does it flag red-flag symptoms, recommend professional medical care,
  and avoid prescribing medications or dosages?
- Appropriate caution: Does it use cautious language and acknowledge
  uncertainty instead of claiming absolute certainty?

Step 3 - Overall score:
- Give an overall score from 1 to 10 that reflects the criteria above.

Step 4 - Feedback:
- List the main strengths of the re-diagnosis.
- List the main weaknesses or gaps.
- Give specific, actionable suggestions to improve it.

Constraints:
- Base your evaluation only on the content of the re-diagnosis above. Do NOT add
  new diagnoses of your own.
- Do NOT provide treatment advice or medication recommendations.
- Be objective, constructive, and specific.
- If the re-diagnosis is empty, unclear, or unusable, give a score of 1 and explain why.

Output format - respond with:
1. Criterion scores (Clarity, Completeness, Reasoning quality, Safety, Appropriate caution), each with a one-line justification
2. Overall score (1-10)
3. Strengths
4. Weaknesses
5. Suggestions for improvement
"""
        out = scoring_agent.kickoff(prompt, response_format=DiagnosisScore)
        if out.pydantic:
            score = out.pydantic  # DiagnosisScore instance
            self.state.score = score.score
        else:
            self.state.score = 0
        return out.raw

    @listen(diagnosis_scoring)
    def report_generator(self, _):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = f"diagnosis_report_{timestamp}.txt"

        with open(report_path, "w", encoding="utf-8") as f:
            f.write("MEDICAL DIAGNOSIS REPORT\n")
            f.write("=" * 40 + "\n\n")
            f.write("INITIAL DIAGNOSIS\n")
            f.write("-" * 40 + "\n")
            f.write(f"{self.state.diagnostics_init}\n\n")
            f.write("RE-DIAGNOSIS\n")
            f.write("-" * 40 + "\n")
            f.write(f"{self.state.rediagnosis}\n\n")
            f.write("SCORE\n")
            f.write("-" * 40 + "\n")
            f.write(f"{self.state.score}\n")

        return report_path


def kickoff():
    content_flow = ContentFlow()
    content_flow.kickoff()


def plot():
    content_flow = ContentFlow()
    content_flow.plot()


def run_with_trigger():
    """
    Run the flow with trigger payload.
    """
    import json
    import sys

    if len(sys.argv) < 2:
        raise Exception("No trigger payload provided. Please provide JSON payload as argument.")

    try:
        trigger_payload = json.loads(sys.argv[1])
    except json.JSONDecodeError:
        raise Exception("Invalid JSON payload provided as argument")

    content_flow = ContentFlow()

    try:
        result = content_flow.kickoff({"crewai_trigger_payload": trigger_payload})
        return result
    except Exception as e:
        raise Exception(f"An error occurred while running the flow with trigger: {e}")


if __name__ == "__main__":
    kickoff()