# File for generic helper functions.
import shutil
from flask import jsonify
from markupsafe import escape  # flask.escape removed in Flask 2.3
from collections import Counter,defaultdict
from metrics_windowing import reduce_video_window
import numpy as np
from scipy.stats import median_abs_deviation
import statistics
import os
import re
import logging
import json

logger = logging.getLogger('utility.funcs')
def string_to_bool(value):
    if not value:
        return None
    return value.lower() in ['true', '1', 't', 'y', 'yes']

def sanitize(value):
    if not value:
        return None
    if isinstance(value, (list,)):
        return [str(sanitize(v)) for v in value]
    else:
        return str(escape(value))

def sanitize_int_value(value):
    if not value or not str(value).isdigit():
        return None
    else:
        return int(value)        

def get_client_ip(request):
    return request.remote_addr

def json_response(payload=None, status=200):
    resp = jsonify(payload if payload is not None else {})
    resp.status_code = status
    # API responses are per-user (Vary: Cookie) and must never be served stale
    # from the browser's heuristic cache — otherwise flags like has_video /
    # has_posthoc and freshly changed data show up late. Force revalidation.
    resp.headers['Cache-Control'] = 'no-store, must-revalidate'
    return resp

def verify_characters(value, chars):
    if re.search(r'^[{0}]+\Z'.format(chars), value):
        return True
    return False


import sys as _sys
_COMMON = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'common')
if _COMMON not in _sys.path:
    _sys.path.insert(0, _COMMON)
from safe_names import safe_name as _safe_name, UnsafeName


def safe_name(value):
    # Filesystem-safe leaf name for building enrollment/biometric paths from a
    # username or alias. Delegates to the canonical common/safe_names.safe_name
    # (same charset/dot/basename guard, which excludes / and \\) and maps its
    # UnsafeName -> None so callers can 404 rather than touch a traversed path.
    try:
        return _safe_name(value)
    except UnsafeName:
        return None

def participant_only_session_prompt(data):
    return f"""
        You are a collaborative learning analytics assistant.
        Your task is to analyze collaboration quality metrics data and return structured feedback.

        -------------------------------------
        OUTPUT FORMAT (MANDATORY)
        -------------------------------------
        Return ONLY valid JSON in this exact structure:

        {{"Session_summary": {{
        "Summary": "...",
        "Sessionpattern":"....",
        "Strongzones": ["...", "..."],
        "Declinezones": ["...", "..."],
        "Strengths": ["...", "..."],
        "Concerns": ["...", "..."],
        "Actions": ["...", "..."],
        "Evidences": ["..","..."],
        "Confidence": "...",
        "Session_metric_summary:{{session_metric_key: "...", session_metric_key:"..."}}"
        }},
        "Window_summary":{{ "window_id" : {{"Summary": "...", "Action": "..."}}, "window_id":{{"Summary": "...","Action": "..."}}}},
        "Group_summary": {{group_metric_key: "...", group_metric_key:"..."}} 
        }}

        -------------------------------------
        DATA TO ANALYZE
        -------------------------------------
        Participant name: 
        {data.get("participant_name", "")}
        
        Participants window by window metric:
        {json.dumps(data.get("participant_level_metric", {}), indent=2)}

        Session Data:
        {json.dumps(data.get("session_level_metric", {}), indent=2)}

        Group summary:
        {json.dumps(data.get("group_level_metric", {}), indent=2)}


        User question:
        {data.get("user_question", "")}

        -------------------------------------
        INSTRUCTIONS
        -------------------------------------
        - Be concise and specific
        - Use only the provided data
        - Do NOT hallucinate missing information
        - Ground all claims in the metrics or moments
        - Provide actionable and practical suggestions
        - Do NOT include any explanation outside JSON
        - Don't incorporate raw metric values in the summary text response, but placed them in the evidence field of the response
        - Ensure the responses are not just reporting observations from the metrics, but drawing insights, synthesis and suggestive conclusions
        - Leverage the window timeline to determine time frame session e.g early, mid or late that are strong or declining zones. Also, you can leverage the trenddirection where 0 means stable, 1 increase and -1 decline
        - In identifying these zones, communicate the response for Strongzone, Declinezone and Sessionpattern in atleast 12 words and not more than 15 words. 
        - Ensure you use formative and suggesting language while communicating the insights
        - Do NOT include markdown or backticks
        - For window_by_window_summary, use the windw by window data in participation_level_metrics to provide summary and action for each window. replace the key window_id with the actual window id
        - For Group_summary, provide synthesized response per metrics in atleast 8 words and not more than 10 word. use same keyname of the group_level_metric object for the key of the response
        - For Session_metric_summary provide synthesized response per metrics in atleast 8 words and not more than 10 word. use same keyname of the session_level_metric object for the key of the response.
        - provide a level of confidence, including an berief explanation of influenced the level of confidence in your analysis. please present it in 'Confidence field' of the structured output
        - Use second person framing
        - Note that all the values for the metrics are scaled to 100 percent except for trendingdirection,gazeontask,word count. This should be considered during the analysis 
        """
        
def participant_interactive_prompt(data):
    return f"""
        You are a collaborative learning analytics assistant.
        Your task is to analyze collaboration quality metrics data and return structured feedback.

        -------------------------------------
        OUTPUT FORMAT (MANDATORY)
        -------------------------------------
        Return ONLY valid JSON in this exact structure:

        {{"Prompt_summary": {{
        "Summary": "...",
        "Computedmetricsused": ["...", "..."],
        "Evidencewindows": ["...","..."],
        "Confidence": "..."
        }}
        }}

        -------------------------------------
        DATA TO ANALYZE
        -------------------------------------
        Participant name: 
        {data.get("participant_name", "")}
        
        window by window metric:
        {json.dumps(data.get("window_level_metric", {}), indent=2)}

        Session Data:
        {json.dumps(data.get("session_level_metric", {}), indent=2)}

        Group summary:
        {json.dumps(data.get("group_level_metric", {}), indent=2)}


        User question:
        {data.get("question", "")}

        -------------------------------------
        INSTRUCTIONS
        -------------------------------------
        - Be concise and specific and provide response in at least 50 words and not more than 100 words
        - Use only the provided data
        - Do NOT hallucinate missing information
        - Ground all claims in the window-by-window containing other participants information, session data and group summary.
        - Provide actionable and practical suggestions
        - Do NOT include any explanation outside JSON
        - Don't incorporate raw metric values in the summary text response, but placed them in the evidence field of the response
        - Ensure the responses are not just reporting observations from the metrics, but drawing insights, synthesis and suggestive conclusions by analyzing all the data provided, finding pattern between participants,
        - across windows, session_data and group summary.
        - For the Evidencewindows, present in it time range formatted in hh:mm:ss - hh:mm:ss using the start time and end time. While for the Computedmetricsused do not list the keys but the actual metric in words
        - Ensure you use formative and suggesting language while communicating the insights. Response should aim to help the student improve and where they did well should also be projected
        - Do NOT include markdown or backticks
        - provide a level of confidence, including an berief explanation of influenced the level of confidence in your analysis. please present it in 'Confidence field' of the structured output
        - Use second person framing
        - Note that all the values for the metrics are scaled to 100 percent except for trendingdirection,gazeontask,word count. This should be considered during the analysis 
        """

def build_prompt(data, type):
    if type == "Session_level analysis for participant":
        return participant_only_session_prompt(data)
    
    if type == "Interactive question answer":
        return participant_interactive_prompt(data)

def compute_median_and_mad(values):
    median = np.median(values)
    mad = median_abs_deviation(values,scale='normal')
    return median, mad

def get_progression(previous, current,type="categorical"):
    if type == "categorical":
        if previous is None:
            return "stable"
        elif current == previous:
            return "stable"
        elif current > previous:
            return "increasing"
        elif current < previous:
            return "decreasing"
    else:
        if previous is None:
            return 0 
        elif current == previous:
            return 0
        elif current > previous:
            return 1
        elif current < previous:
            return -1

def get_class(value, thresholds, classes):
    for i, threshold in enumerate(thresholds):
        if value < threshold:
            return classes[i]
    return  classes[-1]

def convert_attention_to_class_fuse_back_to_accumulator(accumulated_metrics,attention_rates,type="categorical"):
    
    len_attention_rates = len(attention_rates)

    if type == "categorical":
        if len_attention_rates > 0 and len_attention_rates  == 1:
            accumulated_metrics[0][7] = "low"
            return accumulated_metrics
        elif len_attention_rates > 0:
            previous_robust_z = None
            median_x,mad = compute_median_and_mad(np.array(attention_rates))

            if attention_rates[0] <= 0:
                accumulated_metrics[0][7] = "low"
            else:
                robust_z = (attention_rates[0] - median_x) / mad
                accumulated_metrics[0][7] = get_class(robust_z, [-1, 1], ["low", "medium", "high"])+" and "+get_progression(None, robust_z)
                previous_robust_z = robust_z
            for i in range(1, len(attention_rates)):
                x = attention_rates[i]

                if x <= 0:
                    accumulated_metrics[i][7] = "low"
                    continue    

                robust_z = (x - median_x) / mad
                accumulated_metrics[i][7] = get_class(robust_z, [-1, 1], ["low", "medium", "high"])+" and "+get_progression(previous_robust_z, robust_z)
                previous_robust_z = robust_z
           
    return accumulated_metrics

def normalized_value_to_percentage(x):

    # clip first
    x = max(-2, min(2, x))

    if x < -0.5:
        # low: [-2, -0.5] -> [0, 33]
        return ((x - (-2)) / (-0.5 - (-2))) * 33
    elif x <= 0.5:
        # medium: [-0.5, 0.5] -> [34, 66]
        return ((x - (-0.5)) / (0.5 - (-0.5))) * (66 - 34) + 34
    else:
        # high: [0.5, 2] -> [67, 100]
        return ((x - 0.5) / (2 - 0.5)) * (100 - 67) + 67  
    
def compute_average(list_of_values):
    if not list_of_values:
        return 0
    return sum(list_of_values) / len(list_of_values)

def batch_video_metrics(videoMetrics, windowsize, fwrite, speaker, session_device,format='csv'):
    """
    Accumulate video metrics over specified window size.
    """
    accumulated_metrics = []
    start = 0
    end = windowsize 
    newwindowstarted = False  
    facial_emotion = []
    object_on_focus = []
    attention_level = []
    attention_rate_acc = [] 
    l = 0
    window_count = 0
    attention_class = "No Attention"
    n=len(videoMetrics)
    
    while l < n:
        v= videoMetrics[l]
        if not newwindowstarted:
            start = v.time_stamp
            end = start + windowsize

        if v.time_stamp >= start and v.time_stamp < end:
            facial_emotion.append(str(v.facial_emotion))
            object_on_focus.append(str(v.object_on_focus))
            attention_level.append(int(v.attention_level))  
            newwindowstarted = True
            l += 1
        if(v.time_stamp >= start and v.time_stamp >= end) or l == n:
            # Accumulate metrics for the window (shared reducer — was a copy
            # that drifted from the synthesis path on the empty-window value).
            most_common_emotion, most_common_object, avg_attention, attention_rate = \
                reduce_video_window(facial_emotion, object_on_focus, attention_level,
                                    windowsize, window_count)

            # attention_rate_acc.append([window_count, attention_rate])
            attention_rate_acc.append(attention_rate)
        
            accumulated_metrics.append([session_device.id,session_device.name,[start,end],most_common_emotion,
                                        most_common_object,avg_attention,attention_rate,attention_class,speaker.alias,speaker.id])
            # Reset for next window
            facial_emotion = []
            object_on_focus = []
            attention_level = []
            newwindowstarted = False
            window_count += 1         
         
    accumulated_metrics_final = convert_attention_to_class_fuse_back_to_accumulator(accumulated_metrics,attention_rate_acc) 
    if format!='csv':
        return accumulated_metrics_final
    else:
        for metrics in accumulated_metrics_final:
            fwrite.writerow({'Group ID':metrics[0],
                            'Group Name':metrics[1],
                            'Time Range (s)':str(metrics[2][0])+'-'+str(metrics[2][1]),
                            'Facial Emotion': metrics[3],
                            'Object Focus On': metrics[4],
                            'Attention Level': metrics[5],
                            'Attention Rate': metrics[6],
                            "Attention Class":metrics[7],
                            'Speaker Tag': metrics[8],
                            })  

            
def batch_transcript_metrics(transcriptSpeakerMetric, windowsize, fwrite, speaker, session_device,keywords,format='csv'):
    """
    Accumulate video metrics over specified window size.
    """
    accumulated_metrics = []
    start = 0
    end = windowsize 
    newwindowstarted = False  
    analytic = []
    authenticity = []
    certainty = [] 
    clout = []
    emotionaL_tone = []
    participation_score = []
    internal_cohesion = []
    responsivity = []
    social_impact = []
    newness = []
    prev_analytic_value= prev_authenticity_value=prev_certainty_value=prev_clout_value=prev_emotional_tone_value= None
    prev_participation_score_value = prev_internal_cohesion_value = prev_responsivity_value = prev_social_impact_value = prev_newness_value = None
    # communication_density = []
    word_count = []
    words_per_window = []
    keywords_window = []
    keywords_detected_window = []
    similarity_window = []

    l = 0
    n=len(transcriptSpeakerMetric)
    while l < n:
        t, sm = transcriptSpeakerMetric[l]
        if not newwindowstarted:
            start = t.start_time
            end = start + windowsize

        if t.start_time >= start and t.start_time < end:
            analytic.append(int(t.analytic_thinking_value))
            authenticity.append(int(t.authenticity_value))
            certainty.append(int(t.certainty_value))  
            clout.append(int(t.clout_value)),
            emotionaL_tone.append(int(t.emotional_tone_value)),
            participation_score.append(float(sm.participation_score)),
            internal_cohesion.append(float(sm.internal_cohesion)),
            responsivity.append(float(sm.responsivity)),
            social_impact.append(float(sm.social_impact)),
            newness.append(float(sm.newness)), 
            # communication_density.append(float(sm.communication_density)),
            word_count.append(int(t.word_count)),
            words_per_window.append(str(t.transcript)),
            transcript_keywords = [keyword for keyword in keywords if keyword.transcript_id == t.id]
            keywords_window.append(', '.join([keyword.keyword for keyword in transcript_keywords]))
            keywords_detected_window.append(', '.join([keyword.word for keyword in transcript_keywords]))
            similarity_window.append(', '.join([str(round((1-keyword.similarity)*100,3)) for keyword in transcript_keywords]))

            newwindowstarted = True
            l += 1
        if(t.start_time >= start and t.start_time >= end) or l == n:
            # Accumulate metrics for the window
            if analytic:
                val = round(compute_average(analytic), 2)
                analytic_thinking_val = get_class(val, [50, 51], ["Narrative thinking", "Balanced thinking", "Analytical thinking"])  + " and " + get_progression(prev_analytic_value, val)
                prev_analytic_value = val 
            else:
                analytic_thinking_val = 'None'
            if authenticity:
                val = round(compute_average(authenticity), 2)
                authenticity_val = get_class(val, [50, 51], ["Low", "Balanced", "High"]) + " and " + get_progression(prev_authenticity_value, val)
                prev_authenticity_value = val 
            else:
                authenticity_val = 'None'   
            if certainty:
                val = round(compute_average(certainty), 2)
                certainty_val = get_class(val, [50, 51], ["Low", "Medium", "High"]) + " and " + get_progression(prev_certainty_value, val)
                prev_certainty_value = val 
            else:
                certainty_val = 'None'
            if clout:
                val = round(compute_average(clout), 2)
                clout_val = get_class(val, [50, 51], ["Low", "Medium", "High"]) + " and " + get_progression(prev_clout_value, val)
                prev_clout_value = val
            else:
                clout_val = 'None'
            if emotionaL_tone: 
                val = round(compute_average(emotionaL_tone), 2)
                emotional_tone_val = get_class(val, [50, 51], ["Negative", "Neutral", "Positive"]) + " and " + get_progression(prev_emotional_tone_value, val)
                prev_emotional_tone_value = val
            else:
                emotional_tone_val = 'None'
            if participation_score:
                val = round(compute_average(participation_score), 2)
                participation_score_val = get_class(val, [-0.75, -0.25,0.5,1.5], ["Minimal participation" ,"Low participation" , "Balanced participation", "High participation","Dominant participation"]) + " and " + get_progression(prev_participation_score_value, val)
                prev_participation_score_value = val
            else:
                participation_score_val = 'None'
            if internal_cohesion:
                val = round(compute_average(internal_cohesion), 2)
                internal_cohesion_val = get_class(val, [0.2, 0.5], ["Low", "Medium", "High"]) + " and " + get_progression(prev_internal_cohesion_value, val)
                prev_internal_cohesion_value = val
            else:
                internal_cohesion_val = 'None'
            if responsivity:
                val = round(compute_average(responsivity), 2)
                responsivity_val = get_class(val, [0.2, 0.5], ["Low", "Medium", "High"]) + " and " + get_progression(prev_responsivity_value, val)
                prev_responsivity_value = val
            else:
                responsivity_val = 'None'
            if social_impact:
                val = round(compute_average(social_impact), 2)
                social_impact_val = get_class(val, [0.2, 0.5], ["Low", "Medium", "High"]) + " and " + get_progression(prev_social_impact_value, val)
                prev_social_impact_value = val
            else:
                social_impact_val = 'None'
            if newness:
                val = round(compute_average(newness), 2)
                newness_val = get_class(val, [0.2, 0.5], ["Low", "Medium", "High"]) + " and " + get_progression(prev_newness_value, val)
                prev_newness_value = val
            else:
                newness_val = 'None'
            # if communication_density:
            #     val = sum(communication_density)//len(communication_density)
            #     communication_density_val = "High" if val > 0.67 else "Low" if val < 0.33 else "Medium"
            # else:
            #     communication_density_val = 'None'
            if word_count:
                word_count_val = sum(word_count)
            else:
                word_count_val = 0
            if words_per_window:
                transcript_val = ' '.join(words_per_window)
            else:
                transcript_val = ''
            if keywords_detected_window:
                keywords_detected_val = ' '.join(keywords_detected_window)
            else:
                keywords_detected_val = ''
            if similarity_window:
                similarity_val = ' '.join(similarity_window)
            else:
                similarity_val = '' 

            if keywords_window:            
                keywords_val = ' '.join(keywords_window)
            else:
                keywords_val = ''

            if format=='csv':
                    fwrite.writerow({'Group ID':session_device.id,
                            'Group Name':session_device.name,
                            'Time Range (s)': str(start)+'-'+str(end),
                            'Transcript':transcript_val,
                            'Keywords': keywords_val,
                            'Keywords Detected': keywords_detected_val,
                            'Similarity': similarity_val,
                            'Analytic Thinking': analytic_thinking_val,
                            'Authenticity': authenticity_val,
                            'Certainty': certainty_val,
                            'Clout': clout_val,
                            'Emotional Tone': emotional_tone_val,
                            'participation_score': participation_score_val,
                            'internal_cohesion':  internal_cohesion_val,
                            'responsivity':  responsivity_val,
                            'social_impact':  social_impact_val,
                            'newness':  newness_val, 
                            # 'communication_density': communication_density_val,
                            'Word Count': word_count_val,
                            'Speaker Tag': t.speaker_tag,
                            'Speaker ID': int(t.speaker_id),
                            'Topic ID': int(t.topic_id)
                            })
            else:   
                accumulated_metrics.append([session_device.id,session_device.name,[start,end],transcript_val,
                                            keywords_val,keywords_detected_val,similarity_val,analytic_thinking_val,
                                            authenticity_val,certainty_val,clout_val,emotional_tone_val,participation_score_val,
                                            internal_cohesion_val,responsivity_val,social_impact_val,newness_val,
                                            word_count_val,t.speaker_tag,int(t.speaker_id),int(t.topic_id)])
                

            # Reset for next window
            analytic = []
            authenticity = []
            certainty = [] 
            clout = []
            emotionaL_tone = []
            participation_score = []
            internal_cohesion = []
            responsivity = []
            social_impact = []
            newness = []
            # communication_density = []
            word_count = []
            words_per_window = []
            keywords_window = []
            keywords_detected_window = []
            similarity_window = []
            newwindowstarted = False 

    if format!='csv':
        return accumulated_metrics          

 
def batch_transcript_video_metrics(transcriptSpeakerMetric,videoMetrics,windowsize,fwrite,speaker,session_device,keywords,format='csv'):
    """
    Accumulate both transcript and video metrics over specified window size.
    """
    accumulated_metrics = []
    batch_transcript_speaker_metrics = batch_transcript_metrics(transcriptSpeakerMetric,windowsize,fwrite,speaker,session_device,keywords,format='list')
    batch_video_speaker_metrics = batch_video_metrics(videoMetrics,windowsize,fwrite,speaker,session_device,format='list')
    vidLen = len(batch_video_speaker_metrics)
    transLen = len(batch_transcript_speaker_metrics)
    lv,lt = 0,0
    while lv < vidLen and lt < transLen:
        v_metrics = batch_video_speaker_metrics[lv]
        t_metrics = batch_transcript_speaker_metrics[lt]
        # Align by time range
        if v_metrics[2][1] <= t_metrics[2][0]:
            value = [v_metrics[0],v_metrics[1],str(v_metrics[2][0])+'-'+str(v_metrics[2][1]),'','','',
                     '',"None","None","None","None","None","None","None","None","None","None",0,v_metrics[3],v_metrics[4],v_metrics[5],v_metrics[6],v_metrics[7],v_metrics[8],v_metrics[9],-1]
            lv += 1
        elif t_metrics[2][1] <= v_metrics[2][0]:
            value = [t_metrics[0],t_metrics[1],str(t_metrics[2][0])+'-'+str(t_metrics[2][1]),t_metrics[3],t_metrics[4],t_metrics[5],
                     t_metrics[6],t_metrics[7],t_metrics[8],t_metrics[9],t_metrics[10],t_metrics[11],t_metrics[12],t_metrics[13],t_metrics[14],
                     t_metrics[15],t_metrics[16],t_metrics[17],"None","None",0,0,"No Attention",t_metrics[18],t_metrics[19],t_metrics[20]]
            lt += 1
        else:
            # Write combined metrics to CSV or return as list 
            st = min(v_metrics[2][0],t_metrics[2][0])
            en = max(v_metrics[2][1],t_metrics[2][1])
            value = [t_metrics[0],t_metrics[1],str(st)+'-'+str(en),t_metrics[3],t_metrics[4],t_metrics[5],
                     t_metrics[6],t_metrics[7],t_metrics[8],t_metrics[9],t_metrics[10],t_metrics[11],t_metrics[12],t_metrics[13],t_metrics[14],
                     t_metrics[15],t_metrics[16],t_metrics[17],v_metrics[3],v_metrics[4],v_metrics[5],v_metrics[6],v_metrics[7],t_metrics[18],t_metrics[19],t_metrics[20]]
            # Overlapping ranges, process both
            lv += 1
            lt += 1
                  
        write_or_append_metrics(value,fwrite,accumulated_metrics,format=format)
    
    while lv < vidLen:
        v_metrics = batch_video_speaker_metrics[lv]
        value = [v_metrics[0],v_metrics[1],str(v_metrics[2][0])+'-'+str(v_metrics[2][1]),'','','',
                 '',"None","None","None","None","None","None","None","None","None","None",0,v_metrics[3],v_metrics[4],v_metrics[5],v_metrics[6],v_metrics[7],v_metrics[8],v_metrics[9],-1]
        write_or_append_metrics(value,fwrite,accumulated_metrics,format=format)
        lv += 1

    while lt < transLen:
        t_metrics = batch_transcript_speaker_metrics[lt]
        value = [t_metrics[0],t_metrics[1],str(t_metrics[2][0])+'-'+str(t_metrics[2][1]),t_metrics[3],t_metrics[4],t_metrics[5],
                 t_metrics[6],t_metrics[7],t_metrics[8],t_metrics[9],t_metrics[10],t_metrics[11],t_metrics[12],t_metrics[13],t_metrics[14],
                 t_metrics[15],t_metrics[16],t_metrics[17],"None","None",0,0,"No Attention",t_metrics[18],t_metrics[19],t_metrics[20]]
        write_or_append_metrics(value,fwrite,accumulated_metrics,format=format)
        lt += 1    
    
    if format!='csv':
        return accumulated_metrics
def write_or_append_metrics(metrics, fwrite,accumulator, format='csv'):
    """
    Write or append metrics to CSV or return as list.
    """
    if format=='csv':
        fwrite.writerow({'Group ID':metrics[0],
                            'Group Name': metrics[1],
                            'Time Range (s)': metrics[2],
                            'Transcript': metrics[3],
                            'Keywords': metrics[4],
                            'Keywords Detected': metrics[5],
                            'Similarity': metrics[6],
                            'Analytic Thinking': metrics[7],
                            'Authenticity': metrics[8],
                            'Certainty': metrics[9],
                            'Clout': metrics[10],
                            'Emotional Tone': metrics[11],
                            'participation_score': metrics[12],
                            'internal_cohesion':  metrics[13],
                            'responsivity':  metrics[14],
                            'social_impact':  metrics[15],
                            'newness':  metrics[16], 
                            # 'communication_density': metrics[17],
                            'Word Count': metrics[17],
                            'Facial Emotion': metrics[18],
                            'Object Focus On': metrics[19],
                            'Attention Level': metrics[20],
                            'Attention Rate': metrics[21] ,
                            "Attention Class":metrics[22],
                            'Speaker Tag': metrics[23],
                            'Speaker ID': metrics[24],
                            'Topic ID': metrics[25]
                            })
    else:
        accumulator.append(metrics)    


def extract_videometrics_within_window(videoMetrics,speakers, window_start, window_end, index):
    metric_len = len(videoMetrics)
    metrics = defaultdict(dict)
    while index < metric_len: 
        if videoMetrics[index].time_stamp >= window_start and videoMetrics[index].time_stamp < window_end:
            v = videoMetrics[index]
            if  v.student_username not in metrics:
                speakers.add(v.student_username)
                metrics[v.student_username]={'facial_emotion':[str(v.facial_emotion)],'object_on_focus':[str(v.object_on_focus)],'attention_level':[int(v.attention_level)]}
            else:
                metrics[v.student_username]['facial_emotion'].append(str(v.facial_emotion)) 
                metrics[v.student_username]['object_on_focus'].append(str(v.object_on_focus)) 
                metrics[v.student_username]['attention_level'].append(int(v.attention_level)) 
        else:
            break    
        index += 1
    return metrics, index

def extract_transcriptmetrics_within_window(transcriptSpeakerMetric,speakers, keywords,window_start, window_end, index):
    metric_len = len(transcriptSpeakerMetric)
    metrics = defaultdict(dict)

    while index < metric_len: 
        t, sm = transcriptSpeakerMetric[index]
        if t.start_time >= window_start and t.start_time < window_end:
            transcript_keywords = [keyword for keyword in keywords if keyword.transcript_id == t.id]
            keyword_similarity_score = [round((1-keyword.similarity)*100,3) for keyword in transcript_keywords]
            if  t.speaker_tag not in metrics: 
                speakers.add(t.speaker_tag)
                metrics[t.speaker_tag]={'analytic':[int(t.analytic_thinking_value)],
                                        'authenticity':[int(t.authenticity_value)],
                                        'certainty':[int(t.certainty_value)],
                                        'clout':[int(t.clout_value)],
                                        'emotionaL_tone':[int(t.emotional_tone_value)],
                                        'participation_score':[float(sm.participation_score)],
                                        'internal_cohesion':[float(sm.internal_cohesion)],
                                        'responsivity':[float(sm.responsivity)],
                                        'social_impact':[float(sm.social_impact)],
                                        'newness':[float(sm.newness)],
                                        'word_count':[int(t.word_count)],
                                        'words_per_window':[str(t.transcript)],
                                        'keywords':[', '.join([keyword.keyword for keyword in transcript_keywords])],
                                        'keyword_similarity_score':[compute_average(keyword_similarity_score)]}
            else:
                metrics[t.speaker_tag]['analytic'].append(int(t.analytic_thinking_value))
                metrics[t.speaker_tag]['authenticity'].append(int(t.authenticity_value))
                metrics[t.speaker_tag]['certainty'].append(int(t.certainty_value))  
                metrics[t.speaker_tag]['clout'].append(int(t.clout_value)),
                metrics[t.speaker_tag]['emotionaL_tone'].append(int(t.emotional_tone_value)),
                metrics[t.speaker_tag]['participation_score'].append(float(sm.participation_score))
                metrics[t.speaker_tag]['internal_cohesion'].append(float(sm.internal_cohesion))
                metrics[t.speaker_tag]['responsivity'].append(float(sm.responsivity))
                metrics[t.speaker_tag]['social_impact'].append(float(sm.social_impact))
                metrics[t.speaker_tag]['newness'].append(float(sm.newness))
                metrics[t.speaker_tag]['word_count'].append(int(t.word_count)),
                metrics[t.speaker_tag]['words_per_window'].append(str(t.transcript))
                metrics[t.speaker_tag]['keywords'].append(', '.join([keyword.keyword for keyword in transcript_keywords]))
                metrics[t.speaker_tag]['keyword_similarity_score'].append(compute_average(keyword_similarity_score))
        else:
            break    
        index += 1  
    return metrics, index

def aggregate_video_metric_per_window(videoMetrics_per_window,windowsize,window_count,prev_metric):
    facial_emotion = videoMetrics_per_window['facial_emotion']
    object_on_focus = videoMetrics_per_window['object_on_focus']
    attention_level = videoMetrics_per_window['attention_level']

    # Shared reducer — this copy used to emit avg_attention=0 for an empty
    # window while the CSV path emitted None; now both say None ("no data").
    most_common_emotion, most_common_object, avg_attention, attention_rate = \
        reduce_video_window(facial_emotion, object_on_focus, attention_level,
                            windowsize, window_count)
    prev_attention_rate = prev_metric[20]
    trend_direction = get_progression(prev_attention_rate, attention_rate,"numerical")
    gazeontask =  1 if trend_direction == 1 else 0
    return [most_common_emotion,most_common_object,avg_attention,attention_rate,'focus_level',trend_direction,gazeontask]

def aggregate_transcript_metric_per_window(transcriptMetrics_per_window,prev_metric):
    val = []
    analytic = transcriptMetrics_per_window['analytic']
    authenticity = transcriptMetrics_per_window['authenticity']
    certainty = transcriptMetrics_per_window['certainty'] 
    clout = transcriptMetrics_per_window['clout']
    emotionaL_tone = transcriptMetrics_per_window['emotionaL_tone']
    participation_score = transcriptMetrics_per_window['participation_score']
    internal_cohesion = transcriptMetrics_per_window['internal_cohesion']
    responsivity = transcriptMetrics_per_window['responsivity']
    social_impact = transcriptMetrics_per_window['social_impact']
    newness = transcriptMetrics_per_window['newness']
    word_count = transcriptMetrics_per_window['word_count']
    words_per_window = transcriptMetrics_per_window['words_per_window']
    keywords = transcriptMetrics_per_window['keywords']
    keyword_similarity_score = transcriptMetrics_per_window['keyword_similarity_score']
    trend_direction = []
    
    _,_,_,_,_,_,prev_keyword_similarity_score_val, prev_analytic_thinking_val, prev_authenticity_val, prev_certainty_val, prev_clout_val, prev_emotional_tone_val, \
    prev_participation_score_val, prev_internal_cohesion_val,prev_responsivity_val,prev_social_impact_val,prev_newness_val,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_ = prev_metric
    # Accumulate metrics for the window
    if analytic:
        val = round(compute_average(analytic), 2)
        analytic_thinking_val = val
        trend_direction.append(get_progression(prev_analytic_thinking_val, val,"numerical"))
    else:
        analytic_thinking_val = 0
    if authenticity:
        val = round(compute_average(authenticity), 2)
        authenticity_val = val
        trend_direction.append(get_progression(prev_authenticity_val, val,"numerical"))
    else:
        authenticity_val = 0   
    if certainty:
        val = round(compute_average(certainty), 2)
        certainty_val = val
        trend_direction.append(get_progression(prev_certainty_val, val,"numerical"))
    else:
        certainty_val = 0
    if clout:
        val = round(compute_average(clout), 2)
        clout_val = val
        trend_direction.append(get_progression(prev_clout_val, val,"numerical"))
    else:
        clout_val = 0
    if emotionaL_tone: 
        val = round(compute_average(emotionaL_tone), 2)
        emotional_tone_val = val
        trend_direction.append(get_progression(prev_emotional_tone_val, val,"numerical"))
    else:
        emotional_tone_val = 0
    if participation_score:
        val = round(compute_average(participation_score), 2)
        participation_score_val = normalized_value_to_percentage(val)
        trend_direction.append(get_progression(prev_participation_score_val, participation_score_val,"numerical"))
    else:
        participation_score_val = 0
    if internal_cohesion:
        val = round(compute_average(internal_cohesion), 2)
        internal_cohesion_val = val*100
        trend_direction.append(get_progression(prev_internal_cohesion_val, val,"numerical"))
    else:
        internal_cohesion_val = 0
    if responsivity:
        val = round(compute_average(responsivity), 2)
        responsivity_val = val*100
        trend_direction.append(get_progression(prev_responsivity_val, val,"numerical")) 
    else:
        responsivity_val = 0
    if social_impact:
        val = round(compute_average(social_impact), 2)
        social_impact_val = val*100
        trend_direction.append(get_progression(prev_social_impact_val, val,"numerical"))
    else:
        social_impact_val = 0
    if newness:
        val = round(compute_average(newness), 2)
        newness_val = val*100
        trend_direction.append(get_progression(prev_newness_val, val,"numerical"))
    else:
        newness_val = 0
    
    if word_count:
        word_count_val = sum(word_count)
    else:
        word_count_val = 0
    if words_per_window:
        transcript_val = ' '.join(words_per_window)
    else:
        transcript_val = ''
    if keywords:
        keywords_val = ' '.join(keywords)
    else:
        keywords_val = ''

    if keyword_similarity_score:
        val = round(compute_average(keyword_similarity_score), 2)
        keyword_similarity_score_val = val
        trend_direction.append(get_progression(prev_keyword_similarity_score_val, val,"numerical"))
    else:
        keyword_similarity_score_val = 0


    return [transcript_val,word_count_val,keywords_val,keyword_similarity_score_val,analytic_thinking_val,authenticity_val,certainty_val,clout_val,
            emotional_tone_val,participation_score_val, internal_cohesion_val,responsivity_val,social_impact_val,newness_val,trend_direction]
        
def combine_transcript_video_metrics(video_metrics, transcript_metrics,window_id,window_start,window_end):
    # Align video and transcript metrics by time ranges and combine them
    if video_metrics and transcript_metrics:
        most_common_emotion,most_common_object,avg_attention,attention_rate,focus_level,v_trend_direction,gazeontask = video_metrics
        transcript_val,word_count_val,keywords_val,keyword_similarity_score_val,analytic_thinking_val,authenticity_val,certainty_val,clout_val, \
            emotional_tone_val,participation_score_val, internal_cohesion_val,responsivity_val,social_impact_val,newness_val,t_trend_direction = transcript_metrics
        
        t_trend_direction.append(v_trend_direction)
        most_common_trend_direction = Counter(t_trend_direction).most_common(1)[0][0]
        
        return [window_id,window_start,window_end,transcript_val,word_count_val,keywords_val,keyword_similarity_score_val,analytic_thinking_val,authenticity_val,
                certainty_val,clout_val,emotional_tone_val,participation_score_val, internal_cohesion_val,responsivity_val,social_impact_val,newness_val,
                most_common_emotion,most_common_object,avg_attention,attention_rate,gazeontask,'focusscore[-1]','enagementscore[-1]','reasoningscore[-1]',
                'leadershipscore[-1]','initiativescore[-1]','ideacontributionscore[-1]',most_common_trend_direction,'momentum_val','verbalshare_val','turnshare_val']
    
    elif transcript_metrics:
        most_common_emotion = 'None'
        most_common_object = "None"
        avg_attention = 0
        attention_rate = 0
        gazeontask = 0
        transcript_val,word_count_val,keywords_val,keyword_similarity_score_val,analytic_thinking_val,authenticity_val,certainty_val,clout_val, \
            emotional_tone_val,participation_score_val, internal_cohesion_val,responsivity_val,social_impact_val,newness_val,trend_direction = transcript_metrics
        
        most_common_trend_direction = Counter(trend_direction).most_common(1)[0][0]
        return [window_id,window_start,window_end,transcript_val,word_count_val,keywords_val,keyword_similarity_score_val,analytic_thinking_val,authenticity_val,
                certainty_val,clout_val,emotional_tone_val,participation_score_val, internal_cohesion_val,responsivity_val,social_impact_val,newness_val,
                most_common_emotion,most_common_object,avg_attention,attention_rate,gazeontask,'focusscore[-1]','enagementscore[-1]','reasoningscore[-1]',
                'leadershipscore[-1]','initiativescore[-1]','ideacontributionscore[-1]',most_common_trend_direction,'momentum_val','verbalshare_val','turnshare_val']
    
    elif video_metrics:
        most_common_emotion,most_common_object,avg_attention,attention_rate,focus_level,trend_direction,gazeontask = video_metrics

        return [window_id,window_start,window_end,'no speech',0,'no keywords',0,0,0,0,0,0,0,0,0,0,0,
                    most_common_emotion,most_common_object,avg_attention,attention_rate,gazeontask,'focusscore[-1]','enagementscore[-1]',
                    'reasoningscore[-1]','leadershipscore[-1]','initiativescore[-1]','ideacontributionscore[-1]',trend_direction,'momentum_val','verbalshare_val','turnshare_val']
    else:
        return None   

def get_total_verbal_turn_contribution_by_all_person_in_window(wind_metric_acc,persons):
    total_verbal_contributions = 0
    total_turn = 0
    for p in persons:
        total_verbal_contributions += wind_metric_acc[p][4]
        total_turn += 1 if wind_metric_acc[p][4] > 0 else 0

    return total_verbal_contributions,total_turn  

def compute_derived_metric_and_update(metric_acc_per_window,speakerDetail,total_windows,total_verbal_turn_acc,speakeralias,median_x,mad,Combined_object,group_level_metric_acc,total_speaker_detected):
    focusscore=[]
    participationscore=[]
    responsivityscore=[]
    enagementscore=[]
    reasoningscore=[]
    leadershipscore=[]
    initiativescore=[]
    ideacontributionscore=[]
    session_trend = []
    speakingalignmentscore = []
    momentum = []
    turnshare = []
    verbalshare = []
    speaking_word_count = []
    shared_task_focus = []

    # 'angry' was missing, so any window whose dominant facial emotion was
    # angry crashed the whole synthesized-metrics computation (KeyError) and
    # 500'd the reflection dashboard for that pod.
    facial_expression = {'serious':0.9,'neutral':0.8,'surprise':0.7,'happy':0.6,'sad':0.4,'angry':0.3,'fear':0.3,'disgust':0.2}

    
    metric_heading = ['windowid','starttime','endtime','transcript','wordcount','keywords','speaking_alignment','analyticthinking','authenticity','certainty','clout',
               'emotionaltone','participationscore','internalcohesion','responsivityscore','socialimpact','newness','facialemotion','objectfocuson','rawfocusscore',
               'rawfocusrate','gazeontask',"focusscore",'engagementscore','reasoningscore','leadershipscore', 'initiativescore','ideacontributionscore','trenddirection','momentum','verbalshare','turntaking']
    
    previous_trend = None
    for i , data in enumerate(speakerDetail):
        window_id,_,_,transcript_val,word_count_val,keywords_val,keyword_similarity_score_val,analytic_thinking_val,authenticity_val, \
        certainty_val,clout_val,emotional_tone_val,participation_score_val, internal_cohesion_val,responsivity_val,social_impact_val,newness_val, \
        most_common_emotion,most_common_object,_,attention_rate,gazeontask,_,_,_,_,_,_,most_common_trend_direction,_,_,_ = data

        focus_level = 0
        if mad != 0 and most_common_object != "None":
           robust_z = (attention_rate - median_x) / mad 
           focus_level = normalized_value_to_percentage(robust_z)

        speaking = 1 if word_count_val > 0 else 0
        speaking_slience_focus = gazeontask*50+ speaking*50
        facial_emotion = most_common_emotion
        
       
        shared_task_focus.append(gazeontask/total_speaker_detected)
        focusscore.append(round((focus_level+speaking_slience_focus)/2,2))
        speakingalignmentscore.append(keyword_similarity_score_val)
        participationscore.append(participation_score_val)
        responsivityscore.append(responsivity_val)

        total_verbal_contri, total_turn  = get_total_verbal_turn_contribution_by_all_person_in_window(metric_acc_per_window[window_id],metric_acc_per_window[window_id]['persons'])
        comp_verbal_share = word_count_val/total_verbal_contri if total_verbal_contri > 0 else 0
        verbalshare.append(round((comp_verbal_share)*100,2))
        turn = 1 if word_count_val > 0 else 0
        comp_turn = turn/total_turn if total_turn > 0 else 0
        turnshare.append(round((comp_turn)*100,2))

        # .get with a neutral-ish fallback: an unrecognized emotion label from
        # a future model version must degrade the score, not crash the report.
        eng_score = round((focusscore[-1]+participation_score_val+emotional_tone_val+facial_expression.get(facial_emotion, 0.5)+ speaking_slience_focus)/5,2) if word_count_val > 0 and facial_emotion != "None" \
            else  round((focus_level+facial_expression.get(facial_emotion, 0.5)+ gazeontask*100)/3,2)  if facial_emotion != "None" else round((participation_score_val+emotional_tone_val+speaking*100)/3,2)
        enagementscore.append(eng_score)

        reason_score = 0 if word_count_val <= 0  else  round((analytic_thinking_val+certainty_val+internal_cohesion_val)/3,2)
        reasoningscore.append(reason_score)
        
        idea_score = 0 if word_count_val <= 0  else round((analytic_thinking_val+authenticity_val+newness_val)/3,2)
        ideacontributionscore.append(idea_score)

        initia_score = 0 if word_count_val <= 0  else round((certainty_val+participation_score_val+ideacontributionscore[-1])/3,2)
        initiativescore.append(initia_score)

        lead_score = 0 if word_count_val <= 0  else round((clout_val+initiativescore[-1])/2,2)
        leadershipscore.append(lead_score)

        momentum.append(round((focusscore[-1]+participation_score_val+ideacontributionscore[-1])/3,2))

        avg_derivative_metric_val = round((participationscore[-1]+focusscore[-1]+enagementscore[-1]+reasoningscore[-1]+ideacontributionscore[-1]+initiativescore[-1]+leadershipscore[-1]+momentum[-1])/8,2)
        window_trend_dir = get_progression(previous_trend, avg_derivative_metric_val,"numerical")
        previous_trend = avg_derivative_metric_val
        session_trend.append(window_trend_dir)

        if word_count_val > 0:
            speaking_word_count.append(word_count_val)

        #update the derived value
        speakerDetail[i][22] = focusscore[-1]
        metric_acc_per_window[window_id][speakeralias][22] = focusscore[-1]
        speakerDetail[i][23] = enagementscore[-1]
        metric_acc_per_window[window_id][speakeralias][23] = enagementscore[-1]
        speakerDetail[i][24] = reasoningscore[-1]
        metric_acc_per_window[window_id][speakeralias][24] = reasoningscore[-1]
        speakerDetail[i][25] = leadershipscore[-1]
        metric_acc_per_window[window_id][speakeralias][25] = leadershipscore[-1]
        speakerDetail[i][26] = initiativescore[-1]
        metric_acc_per_window[window_id][speakeralias][26] = initiativescore[-1]
        speakerDetail[i][27] = ideacontributionscore[-1]
        metric_acc_per_window[window_id][speakeralias][27] = ideacontributionscore[-1]
        speakerDetail[i][28] = window_trend_dir
        metric_acc_per_window[window_id][speakeralias][28] = window_trend_dir
        speakerDetail[i][29] = momentum[-1]
        metric_acc_per_window[window_id][speakeralias][29] = momentum[-1]
        speakerDetail[i][30] = verbalshare[-1]
        metric_acc_per_window[window_id][speakeralias][30] = verbalshare[-1]
        speakerDetail[i][31] = turnshare[-1]
        metric_acc_per_window[window_id][speakeralias][31] = turnshare[-1]

        if window_id not in Combined_object['window_level']:
            Combined_object['window_level'][window_id] = {speakeralias : dict(zip(metric_heading,speakerDetail[i]))}
        else:
            Combined_object['window_level'][window_id][speakeralias] = dict(zip(metric_heading,speakerDetail[i]))


        if speakeralias not in Combined_object['participants_level']:
            Combined_object['participants_level'][speakeralias] = [dict(zip(metric_heading,speakerDetail[i]))]
        else:
            Combined_object['participants_level'][speakeralias].append(dict(zip(metric_heading,speakerDetail[i])))

 

    len_session_trend = len(session_trend) 
    session_most_common_trend = Counter(session_trend).most_common(1)[0][0] if session_trend else -1
    early_trend = Counter(session_trend[0:len_session_trend//3]).most_common(1)[0][0] if session_trend and len_session_trend >= 3 else -1
    mid_trend = Counter(session_trend[len_session_trend//3 : (len_session_trend//3)*2]).most_common(1)[0][0] if session_trend and len_session_trend >= 3 else -1
    late_trend = Counter(session_trend[(len_session_trend//3)*2 : len_session_trend ]).most_common(1)[0][0] if session_trend and len_session_trend >= 3 else -1
    session_metrics=[]
    session_metrics.append(round(sum(focusscore)/total_windows,2))
    session_metrics.append(round(sum(participationscore)/total_windows,2))
    session_metrics.append(round(sum(responsivityscore)/total_windows,2))
    session_metrics.append(round(sum(enagementscore)/total_windows,2))           
    session_metrics.append(round(sum(reasoningscore)/total_windows,2))
    session_metrics.append(round(sum(leadershipscore)/total_windows,2))
    session_metrics.append(round(sum(initiativescore)/total_windows,2))
    session_metrics.append(round(sum(ideacontributionscore)/total_windows,2))
    session_metrics.append(round(sum(speakingalignmentscore)/total_windows,2))
    session_metrics.append(round(sum(momentum)/total_windows,2))
    # Guard both ratios: a pod whose windows carry no verbal turns (silent
    # recording, unattributed speech) must produce zeros, not a
    # ZeroDivisionError that 500s the whole synthesized report.
    _turn_total = sum(total_verbal_turn_acc)
    session_metrics.append(round((sum(speaking_word_count)/_turn_total)*100,2) if _turn_total else 0)
    session_metrics.append(round((len(speaking_word_count)/len(total_verbal_turn_acc))*100,2) if total_verbal_turn_acc else 0)
    session_metrics.append(session_most_common_trend)
    session_metrics.append(early_trend)
    session_metrics.append(mid_trend)
    session_metrics.append(late_trend)
    session_metrics.append(round((sum(shared_task_focus)/total_windows)*100,2))
    group_level_metric_acc['focusscore'].append(session_metrics[0]/100)
    group_level_metric_acc['participationscore'].append(session_metrics[1]/100)
    group_level_metric_acc['responsivity'].append(session_metrics[2]/100)
    group_level_metric_acc['engagementscore'].append(session_metrics[3]/100)
    group_level_metric_acc['reasoningscore'].append(session_metrics[4]/100)
    group_level_metric_acc['leadershipscore'].append(session_metrics[5]/100)
    group_level_metric_acc['initiativescore'].append(session_metrics[6]/100)
    group_level_metric_acc['ideacontributionscore'].append(session_metrics[7]/100)
    group_level_metric_acc['speakingalignmentscore'].append(session_metrics[8]/100)
    group_level_metric_acc['momentum'].append(session_metrics[9]/100)
    group_level_metric_acc['verbalshare'].append(session_metrics[10]/100)
    group_level_metric_acc['turntaking'].append(session_metrics[11]/100)
    group_level_metric_acc['trenddirection'].append(session_metrics[12])
    group_level_metric_acc['earlytrenddirection'].append(session_metrics[13])
    group_level_metric_acc['midtrenddirection'].append(session_metrics[14])
    group_level_metric_acc['latetrenddirection'].append(session_metrics[15])
    group_level_metric_acc['sharedtaskfocus'].append(session_metrics[16])


    
    session_data_heading = ['avg_focusscore','avg_participationscore','avg_responsivity','avg_engagementscore','avg_reasoningscore','avg_leadershipscore', 'avg_initiativescore','avg_ideacontributionscore',
                            'avg_speakingalignmentscore','avg_momentum','avg_verbalshare','avg_turntaking','avg_trenddirection','earlytrenddirection','midtrenddirection','latetrenddirection','sharedtaskfocus'] 
    Combined_object['session_level'][speakeralias] = dict(zip(session_data_heading,session_metrics))
   
def synthesized_transcript_video_metrics_by_window(transcriptSpeakerMetric,videoMetrics,session_device,keywords,windowsize=10): #speakers,
    v_index = t_index = 0
    attention_rate_acc_per_speaker = defaultdict(list)
    metric_acc_per_speaker = defaultdict(list)
    metric_acc_per_window = defaultdict(dict)
    group_level_metric_acc = defaultdict(list)
    total_verbal_turn_acc = []
    speakers = set()
    Combined_object = {'group_id': session_device.id, 'group_name': session_device.name, 'window_level':{}, 'participants_level':{}, 'session_level':{}, 'group_level':{}}
    min_start = min(videoMetrics[0].time_stamp if videoMetrics else 0, transcriptSpeakerMetric[0][0].start_time if transcriptSpeakerMetric else 0)
    max_end = max(videoMetrics[-1].time_stamp if videoMetrics else 0, transcriptSpeakerMetric[-1][0].start_time if transcriptSpeakerMetric else 0)
    window_start = min_start
    window_end = min_start+windowsize
    window_count = 0
    total_speaker_detected = 0
    while window_start <= max_end:
        window_id = f"w_{window_start}_{window_end}"
        window_video_metrics, v_index = extract_videometrics_within_window(videoMetrics,speakers, window_start, window_end, v_index)
        window_transcript_metrics, t_index = extract_transcriptmetrics_within_window(transcriptSpeakerMetric,speakers,keywords, window_start, window_end, t_index)

        #combine transcrit and video metrics for this window
        for speaker in speakers:
            w_video_meteic = window_video_metrics.get(speaker,[])
            w_transcript_metric = window_transcript_metrics.get(speaker,[])
            prev_metric = metric_acc_per_speaker[speaker][-1] if metric_acc_per_speaker.get(speaker,None) else [None]*32
            aggregated_video_metrics = aggregate_video_metric_per_window(w_video_meteic,windowsize,window_count,prev_metric) if w_video_meteic else []
            aggregated_transcript_metrics = aggregate_transcript_metric_per_window(w_transcript_metric,prev_metric) if w_transcript_metric else []
            
            #accumulate the attention rate for each speaker if there is video metrics. this will be used to compute the robust_z value
            if aggregated_video_metrics:
                attention_rate_acc_per_speaker[speaker].append(aggregated_video_metrics[3]) 

            combine_metrics = combine_transcript_video_metrics(aggregated_video_metrics,aggregated_transcript_metrics,window_id,window_start,window_end)

            if combine_metrics:
                metric_acc_per_speaker[speaker].append(combine_metrics)
                metric_acc_per_window[window_id][speaker]=combine_metrics

                #keep track of all the speaking turns
                if combine_metrics[4] > 0:
                    total_verbal_turn_acc.append(combine_metrics[4])

                if 'persons' not in metric_acc_per_window[window_id]:
                    metric_acc_per_window[window_id]['persons'] = [speaker]
                else:
                    metric_acc_per_window[window_id]['persons'].append(speaker)


        window_start = window_end
        window_end += windowsize
        window_count += 1

    total_speaker_detected = len(metric_acc_per_speaker)
    # so now we begin to compute the derived metrics for each speakers and update the record accordingly
    total_windows = len(metric_acc_per_window)
    for speaker, speakerDetail in  metric_acc_per_speaker.items():
        attention_rates = attention_rate_acc_per_speaker.get(speaker,None)
        median_x,mad = compute_median_and_mad(np.array(attention_rates)) if attention_rates else [0,0]
        compute_derived_metric_and_update(metric_acc_per_window,speakerDetail,total_windows,total_verbal_turn_acc,speaker,median_x,mad,Combined_object,group_level_metric_acc,total_speaker_detected) 
        # session_metric_per_speaker[speaker.alias] = session_metrics  

    if total_speaker_detected > 1:
        #compute group level metric using the accumulated group data data 
        Combined_object['group_level']['verbalparticipationbalance'] = round((1 - statistics.stdev(group_level_metric_acc['verbalshare']))*100,2) 
        Combined_object['group_level']['turntakingbalance'] = round((1 - statistics.stdev(group_level_metric_acc['turntaking'])) * 100,2)
        Combined_object['group_level']['Sharedtaskfocus'] = round(sum(group_level_metric_acc['sharedtaskfocus']),2)
        Combined_object['group_level']['responsivity'] = round((sum(group_level_metric_acc['responsivity'])/total_speaker_detected)*100,2)
        Combined_object['group_level']['ideacontribution'] = round((sum(group_level_metric_acc['ideacontributionscore'])/total_speaker_detected)*100,2)
        Combined_object['group_level']['momentum'] = round((sum(group_level_metric_acc['momentum'])/total_speaker_detected)*100,2)
    
    
    return Combined_object


def disk_free_fraction(path):
    """Free space on the filesystem holding ``path``, as a fraction of its size.
    Recordings, logs and the database all live on the one root filesystem
    today; point this at the recordings volume if they ever move."""
    usage = shutil.disk_usage(path)
    return usage.free / usage.total if usage.total else 0.0
