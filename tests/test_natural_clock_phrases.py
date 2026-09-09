import pytest
from agent.slack_timekeeping import clock_command
from agent.slack_work_intake import switch_target

@pytest.mark.parametrize('text,action', [('Break now','rest'),('Taking lunch','lunch'),("I'm taking a break now.",'rest'),('Starting lunch now!','lunch'),("I'm back",'back'),('back from lunch now','back')])
def test_current_clock_phrases(text, action):
    assert clock_command(text) == (action, '')

@pytest.mark.parametrize('text', ['I took a break at 11', 'Taking lunch tomorrow', 'Break now or later?', 'I am not taking a break now', 'Taking lunch with a customer to discuss work'])
def test_historical_uncertain_or_mixed_phrases_do_not_create_punches(text):
    assert clock_command(text) is None

def test_final_sentence_current_switch():
    assert switch_target('I worked on proposals due Friday. Shifting to CITA in preparation for the 10am call') == 'CITA in preparation for the 10am call'
    assert switch_target('Working on CITA. I might be shifting to GRASP later') is None
