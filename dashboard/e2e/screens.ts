// 승인한 화면 설계 목업의 15화면과 이 앱 주소의 대응
export const SCREENS: [string, string][] = [
  ['MON-01 대기', 'scene=idle'],
  ['MON-01·02·03 요청 진행', 'scene=flow'],
  ['MON-01·04 승인 대기', 'scene=approval'],
  ['MON-01·05 고장 격리', 'scene=fault'],
  ['MON-04 ASK', 'scene=ask'],
  ['MON-05 대여 초과', 'scene=later&panel=device:locker-3'],
  ['MON-06 점유·대여', 'scene=later&panel=uses'],
  ['MON-07 승인', 'scene=later&panel=approvals'],
  ['MON-08 감사 로그', 'scene=later&panel=audit'],
  ['MON-09 이력', 'scene=later&panel=history'],
  ['MON-10 신뢰도 지표', 'scene=later&panel=trust'],
  ['CMN-02 연결 끊김', 'scene=offline'],
  ['SET-01 배치', 'scene=later&view=layout'],
  ['SET-02 폰 연결', 'scene=later&view=phones'],
  ['SET-03 점검', 'scene=later&view=check']
];
export const SIZES: [number, number][] = [[1366, 768], [1920, 1080], [2560, 1440], [3840, 2160]];
