const startBtn = document.querySelector('.start');
const videoLayer = document.querySelector('.video_layer');
const startVideo = videoLayer.querySelector('video');
const introTxt1 = document.querySelector('.intro .txt1');
const introTxt2 = document.querySelector('.intro .txt2');

function startBtnAct() {
  videoLayer.classList.remove('hide');
  startVideo.play();

  startVideo.addEventListener("ended", (event) => {
    location.href = 'files';
  });
}

function introTxtAct() {
  introTxt1.classList.add('act');
  introTxt2.classList.add('act');
  startBtn.classList.add('act');
}

startBtn.addEventListener('click', startBtnAct);

document.addEventListener("DOMContentLoaded", function(){
  introTxtAct();
});